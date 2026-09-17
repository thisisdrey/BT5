### Title
Multisig weight double-counting via ECDSA signature malleability in `ValidateMultiSign` precompile - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The `ValidateMultiSign` TVM precompiled contract (invoked by any smart contract via `validatemultisign(address,uint256,bytes32,bytes[])`) is supposed to sum the weight of each *distinct* authorized key that produced a valid signature over a hash, and compare that sum to `permission.getThreshold()`. Its de-duplication logic only skips a signature if the exact byte sequence was already seen; if the *same address* is recovered again from a *different* (e.g. malleated) signature byte sequence, the code merely throttles CPU usage but still adds the weight again. This lets a caller inflate `totalWeight` using multiple signatures from a single key, bypassing the intended N-of-M signer threshold.

### Finding Description
`ValidateMultiSign.execute()` iterates over the caller-supplied signature array, recovers an address for each, and accumulates weight: [1](#0-0) 

```
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
  ...
  totalWeight += weight;
  executedSignList.add(sign);
  executedSignList.add(recoveredAddr);
}
```

If `recoveredAddr` was already produced by a prior signature in the array, the code only skips the iteration (`continue`) when the *exact same combined `sign` bytes* were already recorded. If the caller instead supplies a **different but still cryptographically valid** signature over the identical hash for the same key (which is trivially producible via ECDSA/secp256k1 malleability — e.g. `(r, n-s)` with the recovery id flipped still recovers to the same address, or any other byte-distinct encoding that recovers to the same address), the `matrixContains(executedSignList, sign)` check fails, so the loop does **not** `continue`. It only calls `MUtil.checkCPUTime()` (a CPU-cost throttle) and then falls through to add `weight` for that address **again**.

This contrasts with the transaction-level multisig check `TransactionCapsule.checkWeight()`, which correctly de-duplicates by *address* (via `addMap.containsKey(base64/address)`) and throws a `PermissionException` ("has signed twice!") regardless of the exact signature bytes: [2](#0-1) 

The precompile's per-address dedup should behave the same way (reject/skip on address collision), but instead it only guards CPU cost, not the security-relevant weight accumulation — an oversight that is the direct analog of the SAML "verify one assertion, but let a second injected copy be used/processed independently" bug class: a single genuinely-signed proof from one key is effectively processed as if it were multiple independent authorizations.

### Impact Explanation
`ValidateMultiSign` is a general-purpose TVM precompile (address family used by `validatemultisign`) that Solidity contracts deployed on TRON commonly use to implement on-chain M-of-N multisig authorization for custody wallets, DAOs, bridges, and other asset-control logic. By supplying two or more distinct-but-malleated signatures that all recover to the same authorized key, an attacker (or a single legitimate but under-threshold signer colluding with themselves) can inflate `totalWeight` past `permission.getThreshold()` without obtaining the required number of independent authorized signers. This is a concrete unauthorized-account-operation vector for any contract relying on `ValidateMultiSign` for threshold-gated actions such as fund release, ownership changes, or withdrawal approval, i.e., unauthorized transfer/theft of funds guarded by such a threshold check.

### Likelihood Explanation
Reachability is trivial: any account can deploy or call a contract that invokes the `validatemultisign` precompile with attacker-controlled `data` (the signature array), reachable from a single signed transaction/contract call — no privileged role required. The only open question is whether TRON's ECDSA signature verification path (`SignUtils.signatureToAddress` / `recoverAddrBySign`) enforces canonical low-`s` signatures; if it does not (this repository's index does not show such a canonicalization check in the recover path used here), producing a second valid, byte-distinct signature over the same hash from the same private key is a standard, well-known ECDSA malleability operation requiring no key compromise beyond already possessing one valid signature (which an attacker frequently does if they are themselves one of the signers, or if a signature was disclosed/broadcast for co-signing).

### Recommendation
In `ValidateMultiSign.execute()`, change the de-duplication to reject (or `continue`/`return DATA_FALSE`) whenever `recoveredAddr` has already contributed weight, regardless of whether the exact signature bytes differ — mirroring `TransactionCapsule.checkWeight()`'s address-based dedup — rather than only throttling CPU time and still accumulating weight. Apply the same fix to `BatchValidateSign` if it has analogous per-address weighting semantics.

### Proof of Concept
1. Key `K` holds weight `w` in a permission with `threshold = T`, where `T > w` but `T <= 2w` (i.e., `K` alone should be insufficient).
2. Attacker (or `K`'s holder) computes `hash = sha256(address || permissionId || data)` as required by the precompile, and signs it once with `K` to get signature `S1 = (r, s, v)`.
3. Attacker derives a second valid signature for the same hash and key using standard secp256k1 malleability: `S2 = (r, n - s, 1 - v)`. `S2` is a different byte sequence but recovers to the same address as `S1`.
4. Attacker calls `validatemultisign(address, permissionId, data, [S1, S2])` from any contract.
5. In the loop: first iteration adds weight `w` for `recoveredAddr(K)`; second iteration recognizes `recoveredAddr` was seen but `sign` (merge of addr+S2) was not, so it does not `continue` — it calls `checkCPUTime()` and adds `weight` (`w`) again, making `totalWeight = 2w >= T`.
6. The precompile returns `DATA_FALSE`→`dataOne()` (success), authorizing an action that should have required a second, independent signer.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1086-1106)
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
```

**File:** chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java (L243-263)
```java
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
