### Title
Duplicate-signature weight amplification in `ValidateMultiSign` TVM precompile allows bypassing multi-sig thresholds - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The CVE describes LibreOffice combining multiple certificate/signature data streams so a viewer sees a validly-signed indicator for content that isn't actually backed by that signature. The java-tron analog is in the `ValidateMultiSign` precompiled contract (address `0x0b`), reachable from any smart contract via TVM: its per-signature deduplication logic can be bypassed by supplying two *different* raw signature byte strings that both recover to the *same* address (e.g. via ECDSA signature malleability, s vs n−s), causing that single key's weight to be counted more than once toward the permission threshold.

### Finding Description
In `PrecompiledContracts.ValidateMultiSign#execute`: [1](#0-0) 

For each supplied signature, the code recovers an address (`recoveredAddr`) and builds a `sign` key as `merge(recoveredAddr, sign)`. It only skips (`continue`) accumulating weight when the *exact* merged byte string (`recoveredAddr + rawSignatureBytes`) has already been seen. If the same address is recovered again but the raw signature bytes differ (i.e., `matrixContains(executedSignList, recoveredAddr)` is true but `matrixContains(executedSignList, sign)` is false), the code does **not** skip — it just calls `MUtil.checkCPUTime()` (a CPU/DoS guard) and falls through to `totalWeight += weight` again for the same address.

This means the intended dedup check is keyed on the raw signature bytes, not on the recovered address. Any signer able to produce two distinct valid signatures over the same hash for their own key (classic ECDSA malleability: for signature `(r, s)`, `(r, n-s)` with the complementary recovery id is also a valid signature recovering to the same address) can have their single key's weight counted twice (or up to `MAX_SIZE = 5` times) when computing `totalWeight` against `permission.getThreshold()`.

By contrast, the on-chain transaction-level dedup in `TransactionCapsule.checkWeight` explicitly guards against this by keying the "already signed" map on the recovered address (`encode58Check(address)`) rather than on the raw signature, once the `VERSION_4_7_1` fork condition is active: [2](#0-1) 
The TVM precompile's own logic, however, was not brought in line with that fix, leaving the weaker byte-level dedup in place.

### Impact Explanation
`ValidateMultiSign` is a general-purpose primitive exposed to any deployed smart contract for verifying that an on-chain multi-sig/permission threshold has been met for arbitrary application data (e.g., custody/treasury contracts, DEX order-signing, escrow releases gated by an account's `Permission`). If a single controlling key can inflate its counted weight by supplying malleable signature variants, a holder of a key whose weight is *below* the configured threshold can nonetheless make the precompile return "valid" (`dataOne()`), causing dependent contract logic to treat an under-authorized action as fully authorized. This can lead to unauthorized execution of privileged operations gated by multisig thresholds implemented via this precompile — i.e., unauthorized account operations / theft of funds guarded by on-chain multisig logic that trusts this precompile's boolean result.

### Likelihood Explanation
Reachable directly by any contract deployer/caller triggering a `TriggerSmartContract` that invokes the `validatemultisign(...)` precompile — no special privilege, no node compromise, and no witness/committee involvement required. Producing a malleable alternate signature for one's own ECDSA key is a standard, well-understood operation (flip `s` to `n-s` and correct the recovery id), so the technique is trivial for anyone in control of the corresponding private key.

### Recommendation
Change the dedup key in `ValidateMultiSign.execute` to the recovered address alone (matching `TransactionCapsule.checkWeight`'s post-fork behavior), rejecting/skipping any second signature that recovers to an address already counted, regardless of whether the raw signature bytes match. Concretely, once `matrixContains(executedSignList, recoveredAddr)` is true, always `continue` (do not add weight again), instead of only skipping on an exact `sign` bytes match.

### Proof of Concept
1. Attacker's account has an `Active` permission with threshold `T`, and attacker's single key has weight `W < T` (not enough alone to pass).
2. Attacker computes `hash = sha256(address || permissionId || data)` as required by the precompile's calling convention.
3. Attacker signs `hash` with their private key to get signature `sig1 = (r, s, v)`.
4. Attacker derives a second, distinct valid signature `sig2 = (r, n-s, v')` (ECDSA malleability) — both `sig1` and `sig2` recover to the same address.
5. Attacker calls `validatemultisign(address, permissionId, data, [sig1, sig2])` from a contract.
6. Inside `ValidateMultiSign.execute`, weight `W` is added for `sig1`; for `sig2`, `recoveredAddr` is already in `executedSignList` but the merged `sign` bytes differ, so the loop does not `continue` and adds `W` a second time, yielding `totalWeight = 2W`.
7. If `2W >= T` (while `W < T`), the precompile returns `dataOne()` (success) even though only one distinct authorized key actually signed, bypassing the intended multi-sig threshold. [3](#0-2)

### Citations

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

**File:** chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java (L248-263)
```java
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
