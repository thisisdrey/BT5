Based on my investigation, I found a concrete signature-weight double-counting flaw in the `ValidateMultiSign` precompiled contract.

### Title
Duplicate-address weight accumulation in `ValidateMultiSign` precompile allows single-key holder to satisfy multi-sig threshold - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
`PrecompiledContracts.ValidateMultiSign.execute` is reachable by any smart contract (deployed by an unprivileged, anonymous account) via a `TriggerSmartContract`/`CreateSmartContract` transaction that calls the precompile at the reserved address. It is meant to let contract logic gate fund releases or permission-based actions on an account's multi-sig threshold, mirroring how `TransactionCapsule.checkWeight` gates on-chain multi-sig transactions.

### Finding Description
In the on-chain (non-VM) signature check, `TransactionCapsule.checkWeight` deduplicates signers by **recovered address** (via a `HashMap` keyed on `encode58Check(address)`), rejecting a transaction if the same address "has signed twice." [1](#0-0) 

In contrast, the TVM precompile `ValidateMultiSign.execute` deduplicates by the pair (address, signature bytes), not by address alone: [2](#0-1) 

The loop only `continue`s (skips adding weight) when the *exact same signature* for an address has already been seen. If the same private key produces a second, *different* valid signature for the same hash — which is trivial via ECDSA malleability (`(r, s)` and `(r, n-s)` both recover to the same address) or simply by using a different `k`/(r,s) pair from repeated signing — `recoveredAddr` is already in `executedSignList`, but the new `sign` (address+sig bytes) is not, so the code falls through, recomputes weight via `TransactionCapsule.getWeight`, and adds it to `totalWeight` again. `ByteArray.matrixContains` in `common/src/main/java/org/tron/common/utils/ByteArray.java:189-196` does a raw byte-array equality check with no notion of address-level dedup.

This lets a single key holder submit two (or more, up to `MAX_SIZE=5`) distinct valid signatures from the *same* key and have their weight counted multiple times, satisfying a `Permission.threshold` that was configured to require multiple independent signers.

### Impact Explanation
Any smart contract that uses `ValidateMultiSign` to authorize fund transfers, permission changes, or other privileged operations gated on an account's configured multi-sig threshold can be bypassed by one signer alone, provided that signer's individual weight is less than the threshold but multiple submitted signatures (from the same key) sum to meet/exceed it. This is effectively an unauthorized-operation/account-takeover primitive for any contract-controlled account relying on this precompile for authorization, directly analogous to the "unauthorized transaction" theme in the external report — funds or state changes intended to require multiple independent approvals can be executed by a single compromised or malicious signer.

### Likelihood Explanation
The precompile is reachable by any address able to deploy or call a smart contract that invokes it — no special privilege is required. Generating a second valid ECDSA signature for the same message hash from the same private key is straightforward (malleable `s`, or simply signing again with different nonce `k` since standard ECDSA signing is not deterministic/RFC6979-enforced in all client SDKs). The `MAX_SIZE = 5` upper bound does not prevent this since only 2 signatures are needed to double a weight.

### Recommendation
In `ValidateMultiSign.execute`, deduplicate strictly by `recoveredAddr` (as `checkWeight` does), rejecting or skipping weight addition whenever `recoveredAddr` has already contributed weight, regardless of whether the exact signature bytes match. Concretely, replace the two-level `executedSignList` (address+sig) check with a single set keyed on `recoveredAddr`, and skip (not merely rate-limit via `checkCPUTime`) whenever the address has already been counted.

### Proof of Concept
1. Configure an account's `Active` permission with `threshold = 2` and two keys: KeyA (weight 1) and KeyB (weight 1), so a genuine transfer needs both KeyA and KeyB to sign.
2. An attacker holding only KeyA computes the transaction/message hash `h` and produces two distinct valid ECDSA signatures for `h`: `sig1 = (r, s)` and `sig2 = (r, n-s)` (canonical malleable pair), both of which recover to KeyA's address via `recoverAddrBySign`.
3. Attacker deploys/calls a contract that invokes the `ValidateMultiSign` precompile with `signatures = [sig1, sig2]`.
4. In the loop: for `sig1`, `recoveredAddr(KeyA)` is not yet in `executedSignList` → weight 1 added, address+sig1 recorded. For `sig2`, `recoveredAddr(KeyA)` **is** already in `executedSignList`, but `sign2` (address+sig2 bytes) is not, so the `continue` is skipped, `getWeight` is called again, and weight 1 is added a second time → `totalWeight = 2 >= threshold(2)`.
5. The precompile returns `dataOne()` (success), authorizing the contract to act as though two independent signers approved, using only KeyA. [3](#0-2)

### Citations

**File:** chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java (L257-263)
```java
      if (ForkController.instance().pass(Parameter.ForkBlockVersionEnum.VERSION_4_7_1)) {
        base64 = encode58Check(address);
      }
      if (addMap.containsKey(base64)) {
        throw new PermissionException(encode58Check(address) + " has signed twice!");
      }
      addMap.put(base64, weight);
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1080-1111)
```java
      AccountCapsule account = this.getDeposit().getAccount(address);
      if (account != null) {
        try {
          Permission permission = account.getPermissionById(permissionId);
          if (permission != null) {
            //calculate weight
            long totalWeight = 0L;
            List<byte[]> executedSignList = new ArrayList<>();
            for (byte[] sign : signatures) {
              byte[] recoveredAddr = recoverAddrBySign(sign, hash);

              sign = merge(recoveredAddr, sign);
              if (ByteArray.matrixContains(executedSignList, recoveredAddr)) {
                if (ByteArray.matrixContains(executedSignList, sign)) {
                  continue;
                }
                MUtil.checkCPUTime();
              }
              long weight = TransactionCapsule.getWeight(permission, recoveredAddr);
              if (weight == 0) {
                //incorrect sign
                return Pair.of(true, DATA_FALSE);
              }
              totalWeight += weight;
              executedSignList.add(sign);
              executedSignList.add(recoveredAddr);
            }

            if (totalWeight >= permission.getThreshold()) {
              return Pair.of(true, dataOne());
            }
          }
```
