### Title
Duplicate-weight counting in `ValidateMultiSign` precompile allows bypassing multisig thresholds via signature malleability - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The `ValidateMultiSign` TVM precompiled contract, reachable by any contract call from an unprivileged caller, is intended to verify that a set of ECDSA signatures collectively satisfy an account's permission threshold (mirroring TRON's on-chain multi-signature/permission model, the same subsystem class implicated in the SIL.Finance incident where a "smart contract permission vulnerability" let a bot bypass intended access controls). The weight-deduplication logic in this precompile only rejects an exact duplicate `(recoveredAddr, signatureBytes)` pair, not a duplicate `recoveredAddr` with a *different* valid signature for the same message — which is trivially producible via ECDSA signature malleability (flipping `s` to `n-s`) or simply re-signing with a non-deterministic nonce.

### Finding Description
In `ValidateMultiSign.execute` [1](#0-0) , for each supplied signature the code recovers the signer address and only "skips" counting weight when the *entire* `(recoveredAddr + signature)` combination has already been seen:

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
```

If the address has already contributed weight but the current `sign` bytes differ (e.g., a malleated signature with `s' = n - s`, or a second signature produced with a different nonce `k`), the `continue` is *not* triggered. Instead, `MUtil.checkCPUTime()` is called (a CPU-time/anti-DoS guard, not a correctness check) and execution falls through to add the same key's `weight` to `totalWeight` a second time. Because a single private-key holder can trivially generate two distinct, both-valid ECDSA signatures over the same `hash` (BouncyCastle's `ECDSASigner` here does not enforce canonical low-S output, and even if it did, a holder can always compute the malleated high-S counterpart), a single signer can supply two entries in the `signatures` array that both recover to their own address and both count toward the multisig `threshold`.

This defeats the security guarantee of the multi-signature `Permission` model (`Permission.threshold`, `Permission.keys`) that this precompile is meant to enforce for the caller's contract-level authorization logic [2](#0-1) . A key with weight `w` under a threshold `T` can now single-handedly satisfy checks requiring `2w`, `3w`, etc., as long as `MAX_SIZE` (5) signatures are supplied.

### Impact Explanation
Any DApp/contract on TVM that uses `ValidateMultiSign` to gate high-value operations (fund release, admin actions, multisig wallets built at the contract level) can have its intended M-of-N (weighted) approval requirement bypassed by a single colluding or compromised signer, who submits their key's signature twice (canonical + malleated, or two independently generated signatures). This is directly analogous to the SIL.Finance root cause: a permission-check flaw silently allowed an unauthorized/insufficiently-authorized actor to pass an authorization gate, enabling unauthorized withdrawal/fund manipulation. Because `ValidateMultiSign` is a generic building block usable by arbitrary deployed contracts for asset custody or governance, this is a fund-theft/unauthorized-operation vector reachable from an ordinary contract call — no privileged role required.

### Likelihood Explanation
Trivial to exploit: it only requires that the attacker control one key that is part of the target `Permission`, and that they submit that key's signature twice with differing bytes (malleated `s`, or a second signing with a different nonce) in the `signatures` array passed to the precompile. No special access, timing, or race condition is needed; it is deterministic and repeatable in a single transaction.

### Recommendation
Change the loop to skip weight counting for any `recoveredAddr` that has already contributed, regardless of whether the raw signature bytes match:
```java
if (ByteArray.matrixContains(executedAddrList, recoveredAddr)) {
    continue; // address already counted, ignore any additional/malleated signature
}
long weight = TransactionCapsule.getWeight(permission, recoveredAddr);
...
executedAddrList.add(recoveredAddr);
```
i.e., track and dedupe strictly by `recoveredAddr`, not by the `(address, signature)` tuple. Additionally, consider enforcing canonical (low-S) signature form during signature parsing/recovery to reduce malleability surface generally.

### Proof of Concept
1. Create an account with an `Active` permission requiring `threshold = 2`, containing a single key `K` with `weight = 2` (or two keys, but attacker controls only one, say `weight = 1` needing threshold 2).
2. Attacker signs `hash = sha256(address || permissionId || data)` once with `K`, producing signature `sig1 = (r, s)`.
3. Attacker computes the malleated signature `sig2 = (r, n - s)` — both `sig1` and `sig2` recover to the same address `K` and are both accepted as valid by `recoverAddrBySign`/ECDSA verification.
4. Attacker calls the `ValidateMultiSign` precompile (e.g., from a TVM contract via `validatemultisign(address, permissionId, data, [sig1, sig2])`) supplying `[sig1, sig2]`.
5. In `execute`, first iteration adds `weight` for `K` (`totalWeight = weight`). Second iteration: `recoveredAddr == K` already in `executedSignList`, but `sign` bytes (`sig2`) differ from `sig1`, so the inner `continue` is skipped; `MUtil.checkCPUTime()` runs, then `weight` is added again (`totalWeight = 2*weight`).
6. `totalWeight >= permission.getThreshold()` now evaluates true using only one real signer, returning `DATA_ONE` (validated) — bypassing the intended 2-signer/weighted requirement. [3](#0-2) 

Note: I was unable to fully inspect `recoverAddrBySign`'s internal signature-recovery implementation (grep only surfaced its call sites in `PrecompiledContracts.java`) or confirm whether BouncyCastle's `ECDSASigner` in `ECKey.java`/`SignUtils.java` enforces canonical low-S signatures anywhere in the verification path; this would need to be checked directly to fully confirm that malleated signatures pass validation, though ECDSA signature malleability (or simply re-signing with a different nonce) makes a second valid signature from the same key trivially obtainable regardless.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1080-1085)
```java
      AccountCapsule account = this.getDeposit().getAccount(address);
      if (account != null) {
        try {
          Permission permission = account.getPermissionById(permissionId);
          if (permission != null) {
            //calculate weight
```

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
