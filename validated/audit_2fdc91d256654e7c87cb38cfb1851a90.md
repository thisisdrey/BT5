### Title
Duplicate signer weight double-counting in `ValidateMultiSign` precompile bypasses multi-sig threshold - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
The TVM precompiled contract `ValidateMultiSign` (analogous to `countValidSignatures` in the referenced report — both sum weights across a signature list to compare against a threshold) contains flawed duplicate-detection logic that can let a single signer's weight be counted more than once, allowing a multisig threshold check to pass without enough distinct signers.

### Finding Description
`ValidateMultiSign.execute` iterates over the caller-supplied signature array, recovers the signer address for each signature, and accumulates `totalWeight` if the address has weight in the target account's `Permission`: [1](#0-0) 

The de-duplication check only skips an entry when *both* the recovered address **and** the exact signature bytes have already been seen:
```
if (ByteArray.matrixContains(executedSignList, recoveredAddr)) {
  if (ByteArray.matrixContains(executedSignList, sign)) {
    continue;
  }
  MUtil.checkCPUTime();
}
long weight = TransactionCapsule.getWeight(permission, recoveredAddr);
...
totalWeight += weight;
executedSignList.add(sign);
executedSignList.add(recoveredAddr);
``` [2](#0-1) 

Because ECDSA signatures are malleable (for a given `(r, s, v)` there exists an alternate valid signature `(r, N-s, 1-v)` that recovers to the exact same address), an attacker holding a single private key can produce two syntactically distinct 65-byte signatures over the same hash that both recover to the same `recoveredAddr`. When such a pair is submitted, `matrixContains(executedSignList, recoveredAddr)` is `true` on the second occurrence, but `matrixContains(executedSignList, sign)` is `false` (different raw signature bytes), so the `continue` is skipped — only `MUtil.checkCPUTime()` (an anti-DoS CPU-time check) runs — and execution falls through to add the same signer's `weight` to `totalWeight` a second time.

This is essentially the same bug class described in the external report for `countValidSignatures`: no protection against a signer address appearing more than once (with a different signature encoding), so `totalWeight` inflates beyond the number of genuinely distinct authorized signers.

By contrast, the equivalent on-chain transaction-broadcast signature path, `TransactionCapsule.checkWeight`, correctly guards against duplicate signers by tracking recovered addresses in a map and throwing `PermissionException` if an address is seen twice, regardless of signature byte encoding: [3](#0-2) 
This confirms the intended semantics (one signer = one weight contribution, keyed by recovered address, not by raw signature bytes) and that `ValidateMultiSign`'s address-plus-signature-bytes dedup check deviates from that safe pattern.

### Impact Explanation
`ValidateMultiSign` is a TVM precompile directly callable from any smart contract by any unprivileged account (via a `TriggerSmartContract`/contract call). It is intended to let on-chain contracts verify that a set of TRON multi-sig permission holders (e.g. a wallet contract's active-permission keys) meet a required weight threshold before authorizing an action (e.g. releasing funds, executing a privileged operation gated by the precompile's boolean result). If a caller with just one authorized key (weight `w < threshold`, but `2w >= threshold`) can submit two malleable-signature variants of their own signature, `totalWeight` will double-count and can reach/exceed `permission.getThreshold()`, causing the precompile to return "true" (valid) even though only one real signer authorized the action. Any contract relying on this precompile's result to gate an operation (fund release, permission-based execution, etc.) can be tricked into acting on an insufficient number of distinct approvals — this is an unauthorized-operation / theft-of-funds class impact for any contract built on this primitive.

### Likelihood Explanation
The attack requires only: (1) a valid `Permission` in which the attacker controls at least one key, and (2) the ability to compute a second malleable ECDSA signature for the same hash/private key, which is a well-known, computationally trivial transformation (`s' = N - s`, `v' = 1 - v`). No special privileges are needed — any account/contract that can call the precompile (any address, since `TVM` calls are generally permissionless) can construct this input. The precondition of needing `2w >= threshold` with `w < threshold` limits blast radius to permissions configured with such weight/threshold ratios (e.g., 2-of-3 style setups where a single key's weight is over half the threshold), but this is a common configuration pattern, so likelihood is non-trivial for affected deployments.

### Recommendation
In `ValidateMultiSign.execute` (and the analogous logic in `BatchValidateSign` if it has the same pattern), deduplicate strictly by `recoveredAddr`, not by the combination of address and raw signature bytes — mirroring `TransactionCapsule.checkWeight`'s address-keyed map. Specifically, change the check to `continue` (skip weight addition) whenever `recoveredAddr` has already been recorded, regardless of whether the exact signature bytes differ, e.g.:
```
if (ByteArray.matrixContains(executedSignList, recoveredAddr)) {
  continue;
}
```
and only track `recoveredAddr` in `executedSignList` (drop the now-unnecessary `sign` bytes tracking used for the flawed dedup check).

### Proof of Concept
1. Deploy/target an account with an active `Permission` containing key `A` with weight `w` and threshold `T` such that `w < T <= 2*w` (e.g., `w = 5`, `T = 8`).
2. Off-chain, sign the hash `Sha256Hash.hash(address || permissionId || data)` once with `A`'s private key to get signature `sig1 = (r, s, v)`.
3. Derive the malleable counterpart `sig2 = (r, N - s, 1 - v)` — this is a standard secp256k1 transform and recovers to the same address `A`.
4. Call the `ValidateMultiSign` precompile (via a `TriggerSmartContract`) supplying `signatures = [sig1, sig2]`.
5. Trace through `execute`: first iteration adds `weight=5` for `A`; second iteration's `recoveredAddr` matches but `sign` bytes differ from `sig1`, so the inner `continue` is skipped, `MUtil.checkCPUTime()` runs, and `weight=5` is added again, making `totalWeight = 10 >= threshold(8)`, so the precompile returns `dataOne()` (valid) — see: [4](#0-3) 
6. A contract gating a privileged action on this precompile's boolean result is thus fooled into believing two independent signers (weight 10) authorized the action, when in fact only one key (`A`, true weight 5) signed.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1086-1109)
```java
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
```

**File:** chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java (L242-263)
```java
    HashMap addMap = new HashMap();
    for (ByteString sig : sigs) {
      if (sig.size() < 65) {
        throw new SignatureFormatException(
            "Signature size is " + sig.size());
      }
      String base64 = TransactionCapsule.getBase64FromByteString(sig);
      byte[] address = SignUtils
          .signatureToAddress(hash, base64, CommonParameter.getInstance().isECKeyCryptoEngine());
      long weight = getWeight(permission, address);
      if (weight == 0) {
        throw new PermissionException(
            ByteArray.toHexString(hash) + " is signed by " + encode58Check(address)
                + " but it is not contained of permission.");
      }
      if (ForkController.instance().pass(Parameter.ForkBlockVersionEnum.VERSION_4_7_1)) {
        base64 = encode58Check(address);
      }
      if (addMap.containsKey(base64)) {
        throw new PermissionException(encode58Check(address) + " has signed twice!");
      }
      addMap.put(base64, weight);
```
