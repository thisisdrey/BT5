### Title
Multi-signature Threshold Bypass via Signature-Malleability Double-Counting in `ValidateMultiSign` Precompile - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The `ValidateMultiSign` TVM precompiled contract, reachable by any smart-contract call (a TVM opcode invoked by an unprivileged contract caller), verifies whether a set of signatures meets an account's permission threshold. The de-duplication logic used to prevent a single key from contributing weight multiple times keys off the exact signature bytes rather than the recovered signer address, so a single private key can be counted twice (or more) toward the threshold by supplying two distinct, still-valid ECDSA signatures (e.g. malleable `s`/`v` variants) of the same message hash.

### Finding Description
In `PrecompiledContracts.ValidateMultiSign.execute` [1](#0-0) , for each supplied signature the code:
1. Recovers the signer address `recoveredAddr`.
2. Concatenates `recoveredAddr` with the raw `sign` bytes into `sign`.
3. Skips the signature only if the *exact* `sign` value (address+signature bytes) has already been recorded; if `recoveredAddr` is already present but paired with a different signature byte string, execution falls through to `MUtil.checkCPUTime()` and then still adds the weight again via `TransactionCapsule.getWeight(permission, recoveredAddr)`.

This means the guard `ByteArray.matrixContains(executedSignList, recoveredAddr)` only gates a CPU-time check, not weight accrual — the only real de-dup is on the identical signature bytes. Because ECDSA signatures are malleable (a valid `(r, s)` pair for message `m` can be transformed into another valid `(r, -s mod n)`/different `v` pair that still recovers to the same address, and Tron's crypto layer does not appear to canonicalize `s` before this check), the same private key can be used to produce multiple distinct byte-level signatures over the identical `hash`. Each such signature recovers to the same `recoveredAddr` but is treated as a "new" entry, so `totalWeight` accumulates the key's weight once per malleable variant supplied — up to `MAX_SIZE` (5) signatures.

Contrast this with the account-level multisig verification path `TransactionCapsule.checkPermission`/`checkWeight` used for ordinary transaction signature validation [2](#0-1) , where deduplication is intended to be per-signer. The `ValidateMultiSign` precompile — which is exposed to arbitrary smart contracts via TVM opcode and is explicitly designed to let a contract check an off-chain multi-signature threshold, e.g. for custody/escrow logic — implements the equivalent authorization check but with the weaker, bypassable de-dup key.

This is analogous to the OpenFGA bug class: a security-critical check ("has this set of principals met the authorization threshold?") is bypassed because the verification treats what should be a *single identity relationship* (one key = one unit of weight) as if distinct low-level artifacts (different signature encodings) represent independent relationships, allowing the same principal to be "double-counted" toward satisfying a policy it should not satisfy on its own.

### Impact Explanation
Any smart contract that relies on the `ValidateMultiSign` precompile to gate a sensitive on-chain action (e.g., releasing escrowed TRX/TRC-20 tokens, authorizing a multisig-protected contract operation) can be tricked into believing an M-of-N threshold has been met when in reality fewer than the required number of distinct keys participated — a single colluding or compromised key can satisfy the threshold alone by supplying multiple malleable signature variants. This is a concrete authorization bypass that can lead to unauthorized approval of an on-chain operation (e.g., unauthorized transfer/release of funds gated by the contract's multisig logic), i.e., theft or unauthorized account operation of funds controlled by the calling contract's business logic.

### Likelihood Explanation
The precompile is reachable by any caller that can deploy or invoke a smart contract using the `validatemultisign` TVM opcode — no special privileges are required. Constructing a malleable signature variant from an existing ECDSA signature is a well-known, low-cost operation (flipping `s` to `n - s` and adjusting the recovery id), so exploitation does not require the private key holder's active cooperation beyond producing one legitimate signature, and it does not require access to unavailable secrets. The main dependency is on real-world usage: exploitability requires a contract that uses this precompile's result to gate value-affecting logic with a threshold > 1 among a small key set, which is exactly the precompile's intended use case.

### Recommendation
Change the de-duplication key in `ValidateMultiSign.execute` from the concatenation of `recoveredAddr + sign` to `recoveredAddr` alone, so that once an address has contributed weight it is skipped/rejected regardless of which raw signature bytes were used to reach it. Additionally, canonicalize/normalize signatures (enforce low-`s` form and correct recovery id) before address recovery to eliminate ECDSA malleability. Align this logic with the intended semantics of `TransactionCapsule.checkPermission`/`checkWeight`, which are meant to count each distinct signer's weight exactly once.

### Proof of Concept
1. Deploy a contract that calls the `validatemultisign(address, permissionId, hash, signatures[])` precompile against an account whose `Active` permission has `threshold = 2` split across two keys, key A (weight 1) and key B (weight 1).
2. Have only key A sign `hash` once normally, producing signature `sigA1`.
3. Derive a malleable variant `sigA2` of `sigA1` for the same `hash` (flip `s -> n - s`, flip recovery id) — `sigA2` still recovers to key A's address and is a valid signature.
4. Call the precompile with `signatures = [sigA1, sigA2]` (only one real key, no participation from key B).
5. In `execute`, the first iteration adds weight 1 for A and records `sign1 = A||sigA1`. On the second iteration, `recoveredAddr` (A) is already present in `executedSignList`, but `sign2 = A||sigA2` is not identical to `sign1`, so the `continue` short-circuit is skipped, and `TransactionCapsule.getWeight(permission, A)` is added again — `totalWeight` becomes 2, meeting/exceeding `threshold = 2` even though key B never participated. The precompile incorrectly returns `dataOne()` (success) [3](#0-2) .

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1086-1110)
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
            }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java (L635-645)
```java
  private static void checkPermission(int permissionId, Permission permission, Transaction.Contract contract) throws PermissionException {
    if (permissionId != 0) {
      if (permission.getType() != PermissionType.Active) {
        throw new PermissionException("Permission type is error");
      }
      //check operations
      if (!checkPermissionOperations(permission, contract)) {
        throw new PermissionException("Permission denied");
      }
    }
  }
```
