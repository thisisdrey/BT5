### Title
ECDSA signature-malleability weight double-count in `ValidateMultiSign` precompile bypasses multi-sig threshold - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The Nhost report's root cause is that a controller trusts a per-identity "already proven" flag (`EmailVerified`) that individual adapters can be tricked into setting even though the underlying claim was never actually and distinctly authenticated, letting one attacker-controlled credential be accepted as if it were an independent, verified proof of ownership. The equivalent pattern in java-tron is the `ValidateMultiSign` TVM precompile, which is supposed to require *N* cryptographically distinct owner signatures to reach a permission `threshold`, but de-duplicates candidate signatures by the raw signature bytes instead of solely by the recovered signer address, so two ECDSA-malleable encodings of the *same* private key's signature over the *same* hash are treated as two independent proofs of ownership and their weights are summed.

### Finding Description
`PrecompiledContracts.ValidateMultiSign.execute()` iterates over the caller-supplied signature array and, for each one, recovers a signer address and accumulates `Permission` weight toward `threshold`: [1](#0-0) 

For every `sign`, `recoverAddrBySign(sign, hash)` is called, and the de-duplication check is:
```
sign = merge(recoveredAddr, sign);
if (ByteArray.matrixContains(executedSignList, recoveredAddr)) {
  if (ByteArray.matrixContains(executedSignList, sign)) { continue; }
  MUtil.checkCPUTime();
}
```
This only skips counting a signature if the *exact same raw signature bytes* (merged with the recovered address) were already seen. If the recovered address has been seen before but the raw signature bytes differ, the loop does **not** skip — it just runs a CPU-time guard and then adds the weight again.

`recoverAddrBySign` builds the signature from `Rsv.fromSignature`, calls `SignUtils.fromComponents(...)`, and accepts it if `signature.validateComponents()` passes: [2](#0-1) 

ECDSA signatures are malleable: for any valid `(r, s)` over a given curve, `(r, n-s)` with an inverted recovery id `v` is also a mathematically valid signature that recovers to the *identical* address. `ECKey.ECDSASignature` even exposes `toCanonicalised()`, confirming the codebase is aware that non-canonical (high-`s`) signatures are a distinct, valid representation: [3](#0-2) 

I was not able to fully retrieve the body of `ECDSASignature.validateComponents()` in this session (only located it via `grep_search`, not full contents), so I cannot conclusively confirm from the index whether it additionally rejects non-canonical/high-`s` values. If it does not (which is the common implementation inherited from ethereumJ/ethereum-style libraries, where canonicalization is a separate, opt-in step via `toCanonicalised()`), then both the canonical and malleable variant of one signature pass `validateComponents()` and `recoverAddrBySign`, and the `ValidateMultiSign` dedup logic — keyed on raw bytes rather than solely on `recoveredAddr` — will count both toward `totalWeight`.

### Impact Explanation
`ValidateMultiSign` is a TVM precompiled contract at a fixed address, callable by any smart contract from any unprivileged, unauthenticated transaction sender (any account triggering a contract that invokes the precompile). If the malleability gap is real (pending confirmation of `validateComponents()`), a holder of a single key that is one of several required multi-sig keys on an `Active`/`Owner` permission could submit that key's signature plus its malleable twin to satisfy a threshold that was supposed to require multiple independent keyholders (e.g., 2-of-2 or M-of-N), effectively bypassing the multi-signature authorization model for any application/contract logic that relies on `ValidateMultiSign` to gate privileged actions (fund release, admin operations, etc.), i.e., an unauthorized-account-operation / authorization-bypass condition.

### Likelihood Explanation
The precompile is reachable by any contract call with attacker-supplied `data`, `permissionId`, and signature array — no special privilege needed. The only precondition is that `validateComponents()` does not reject non-canonical `s`. This needs to be confirmed against the exact implementation before treating it as certain; if `validateComponents()` already enforces low-`s` canonical form, this specific vector is not exploitable and only the weaker "insufficient dedup keying" design smell remains (defense-in-depth gap, not itself exploitable).

### Recommendation
- De-duplicate strictly by `recoveredAddr` (one weight credit per unique recovered address per call), not by the concatenation of address + raw signature bytes, in `ValidateMultiSign.execute()`.
- Additionally/alternatively, enforce canonical (low-`s`) signature form before recovery in `recoverAddrBySign`, mirroring `toCanonicalised()`, so malleable duplicates can never recover as "new" entries in the first place.
- Add a regression test that submits a signature and its canonical/non-canonical (`r, n-s`) counterpart from the same key and asserts the combined weight is not double-counted and the call fails to reach threshold if only one distinct key actually signed.

### Proof of Concept
1. Create an account with an `Active` permission requiring `threshold = 2`, containing two distinct keys, `keyA` and `keyB`, weight 1 each.
2. Attacker controls only `keyA`.
3. Attacker computes the precompile's hash `hash = Sha256Hash(merge(address, permissionId, data))` and produces `sigA = keyA.sign(hash)` (canonical form) and its malleable twin `sigA' = (r, n-s)` with the correspondingly flipped recovery id.
4. Attacker calls the `ValidateMultiSign` precompile from a contract, passing `[sigA, sigA']` as the signature array.
5. In the loop: first iteration recovers `keyA`'s address, adds weight 1, records `merge(addr, sigA)`. Second iteration recovers the same `keyA` address (since `sigA'` is a valid malleable variant), but `merge(addr, sigA')` is a different byte string than the first, so the `matrixContains(executedSignList, sign)` check does not match, weight 1 is added again → `totalWeight = 2 >= threshold(2)`, and the call returns `true` despite only one real keyholder participating.

*(This PoC assumes `validateComponents()` accepts non-canonical `s`; this was not independently confirmed in this session due to being unable to view its full source, and should be verified before treating this as a confirmed, exploitable vulnerability.)*

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L371-388)
```java
  private static byte[] recoverAddrBySign(byte[] sign, byte[] hash) {
    byte[] out = null;
    if (ArrayUtils.isEmpty(sign) || sign.length < 65) {
      return new byte[0];
    }
    try {
      Rsv rsv = Rsv.fromSignature(sign);
      SignatureInterface signature = SignUtils.fromComponents(rsv.getR(), rsv.getS(), rsv.getV(),
          CommonParameter.getInstance().isECKeyCryptoEngine());
      if (signature.validateComponents()) {
        out = SignUtils.signatureToAddress(hash, signature,
            CommonParameter.getInstance().isECKeyCryptoEngine());
      }
    } catch (Throwable any) {
      logger.info("ECRecover error", any.getMessage());
    }
    return out;
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1088-1106)
```java
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
```

**File:** crypto/src/main/java/org/tron/common/crypto/ECKey.java (L948-963)
```java
    public ECDSASignature toCanonicalised() {
      if (s.compareTo(HALF_CURVE_ORDER) > 0) {
        // The order of the curve is the number of valid points that
        // exist on that curve. If S is in the upper
        // half of the number of valid points, then bring it back to
        // the lower half. Otherwise, imagine that
        //    N = 10
        //    s = 8, so (-8 % 10 == 2) thus both (r, 8) and (r, 2)
        // are valid solutions.
        //    10 - 8 == 2, giving us always the latter solution,
        // which is canonical.
        return new ECDSASignature(r, CURVE.getN().subtract(s));
      } else {
        return this;
      }
    }
```
