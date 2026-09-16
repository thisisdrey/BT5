### Title
Multi-sig weight double-counting via malleable/duplicate signature bytes in `ValidateMultiSign` precompile - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The `ValidateMultiSign` TVM precompile de-duplicates signatures by comparing the exact signature bytes rather than the recovered signer address, so a single key holder can supply multiple distinct-but-valid signatures over the same hash to have their weight counted more than once, allowing the multi-sig threshold to be satisfied without the required number of distinct authorized signers.

### Finding Description
`ValidateMultiSign.execute` recovers the signer address for every submitted signature and accumulates `totalWeight` against `permission.getThreshold()`: [1](#0-0) 

The de-duplication logic only skips a signature when the *exact byte-identical* signature for that address was already processed:
```
if (matrixContains(executedSignList, recoveredAddr)) {
  if (matrixContains(executedSignList, sign)) { continue; }
  MUtil.checkCPUTime();
}
weight = getWeight(permission, recoveredAddr);
totalWeight += weight;
```
If the same address appears again with a *different* signature byte-encoding (e.g., produced via ECDSA signature malleability — flipping `s` to `n-s` and adjusting the recovery id, which any holder of the private key can trivially compute without re-signing), the inner `matrixContains(executedSignList, sign)` check fails to match, so execution falls through and adds `weight` for that same address a second time. Nothing in the loop caps contributions per unique `recoveredAddr`; only exact-signature repeats are filtered.

By contrast, the equivalent on-chain transaction-signature verification path in `TransactionCapsule.checkWeight` de-duplicates by the recovered **address** (via a `HashMap` keyed on `encode58Check(address)`), which is the correct semantics: [2](#0-1) 

The precompile's independent, weaker re-implementation of the same weight-counting logic diverges from this and is exposed directly to TVM callers.

### Impact Explanation
`ValidateMultiSign` is a public precompiled contract callable by any smart contract (and thus by any unprivileged EOA that triggers a contract call), used by dApps to verify off-chain multi-sig approvals for e.g. custodial/escrow logic. An attacker controlling a single key belonging to a multi-sig `Permission` (e.g., a weight-1 key in a threshold-N scheme) can craft `N` distinct signatures from that one key (through trivial ECDSA malleability, no need to compromise other keys) and pass them all in the `signatures` array. The precompile will report weight ≥ threshold and return `true` (`dataOne()`), causing dependent contract logic to treat the operation as authorized by multiple independent signers when only one key actually approved it. This is an authentication-bypass class bug — analogous to the OpenVPN issue where independently-evaluated deferred checks were incorrectly combined to grant access on partial credentials — and can lead to unauthorized fund release/theft in any contract that relies on this precompile to gate value transfers or privileged actions.

### Likelihood Explanation
Exploitation only requires: (1) being one authorized keyholder (even at minimal weight) of a `Permission` used by a smart contract relying on `ValidateMultiSign`, and (2) generating additional valid-but-different signature encodings for the same hash from that key, which is a well-known, computationally trivial ECDSA property (no brute force or private-key recovery needed). No special privileges, timing races, or network position are required — a single crafted transaction/contract call is sufficient. The bug is reachable purely through the standard TVM call path into the precompile registry.

### Recommendation
Change the de-duplication check in `ValidateMultiSign.execute` to key exclusively on `recoveredAddr` (as `TransactionCapsule.checkWeight` does), rejecting or skipping any signature whose signer address has already contributed weight in the current call, regardless of the raw signature bytes. Additionally consider enforcing canonical (low-S) signature encoding during recovery to reduce trivial malleability surfaces across all signature-verification paths.

### Proof of Concept
1. Create/control an account with an `Active` `Permission` of `threshold = 3`, containing keys `K1` (weight 1), `K2` (weight 1), `K3` (weight 1) — attacker only controls `K1`.
2. Compute `hash = SHA256(address || permissionId || data)` as done in the precompile.
3. Using `K1`'s private key, produce three distinct valid signatures over `hash`: `sig1` (raw), `sig2` = malleated form of `sig1` (`s' = n - s`, recovery id flipped), `sig3` = a fresh non-deterministic signature over `hash` (any ECDSA signer using randomized `k` yields a different valid `(r, s)` per call) — all three recover to `K1`'s address.
4. Call the `ValidateMultiSign` precompile (via a TVM contract) with `signatures = [sig1, sig2, sig3]`.
5. In `execute`, each signature recovers to the same `recoveredAddr` (`K1`), but since `sign` (address+signature bytes) differs each time, the `matrixContains(executedSignList, sign)` check never matches, so `totalWeight` accumulates `1+1+1 = 3 ≥ threshold`, and the call returns `dataOne()` (success) even though only one real signer (`K1`) approved the action. [3](#0-2)

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
