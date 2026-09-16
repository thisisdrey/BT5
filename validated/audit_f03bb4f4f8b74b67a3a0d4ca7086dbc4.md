### Title
Signature malleability in `ValidateMultiSign` precompile allows double-counting a single key's weight to bypass multisig thresholds - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
`ECKey.ECDSASignature.validateComponents` only checks that `r` and `s` are within `[1, SECP256K1N)`; it never enforces the canonical low-`s` rule [1](#0-0) . Because ECDSA signatures are inherently malleable (both `(r, s)` and `(r, N-s)` are valid for the same key/hash), any private key can produce two different byte-level signature encodings that recover to the same address. The `ValidateMultiSign` TVM precompile's weight-aggregation loop deduplicates by comparing the exact merged signature bytes, not solely by recovered address, allowing the same key's weight to be counted twice using a malleated signature pair.

### Finding Description
In `ValidateMultiSign.execute`, for each supplied signature the address is recovered and weight is looked up and added unconditionally unless the *exact* `merge(recoveredAddr, sign)` byte value was already seen: [2](#0-1) 

The dedup check is:
```
if (ByteArray.matrixContains(executedSignList, recoveredAddr)) {
  if (ByteArray.matrixContains(executedSignList, sign)) {
    continue;   // skip only if identical signature bytes repeated
  }
  MUtil.checkCPUTime();   // otherwise: falls through and still adds weight again
}
```
`ByteArray.matrixContains` performs a raw `Arrays.equals` byte comparison [3](#0-2) , so it treats a malleated variant of a signature (different `s`/`v` bytes, same recovered address) as a "new" signature rather than a duplicate. Because `ECDSASignature.validateComponents()` does not reject the non-canonical high-`s` form [4](#0-3) , an attacker holding a single private key that is a member of a multi-sig `Permission` can:
1. Sign the message hash once with their key, producing `(r, s, v)`.
2. Compute the malleated variant `(r, N-s, v')` (a purely mechanical transformation, no private key knowledge beyond the original signature needed).
3. Submit both signatures as the `signatures` array argument to the `ValidateMultiSign` precompile (invoked from a smart contract via `TriggerSmartContract`, i.e., reachable from any unprivileged transaction sender).

Both signatures pass `signature.validateComponents()` (used indirectly via `recoverAddrBySign`, matching the same validation logic as `ECRecover`) and both recover to the same address, but since their raw bytes differ, the loop adds `weight` for that single key **twice**, inflating `totalWeight` past the permission's `threshold` even though only one real key participated. The same `getEnergyForData`/`checkWeight` style dedup on decoded addresses used elsewhere (`TransactionCapsule.checkWeight`, post fork `VERSION_4_7_1`) deduplicates by `encode58Check(address)` rather than raw signature bytes [5](#0-4) , showing that the correct fix (dedup by recovered address) was already applied to the transaction-level signature-weight path but was not applied consistently to the `ValidateMultiSign` TVM precompile.

### Impact Explanation
Any smart contract that relies on `ValidateMultiSign` (address `0x0...1` TVM precompile) to gate privileged operations (e.g., a custom multisig wallet or vault contract built on top of an on-chain `Permission`) can be bypassed by a holder of a single authorized key, satisfying a threshold that should require multiple independent signers. This directly enables unauthorized execution of privileged contract logic gated by `ValidateMultiSign`, which can lead to unauthorized asset transfers/theft depending on how the calling contract uses the boolean result.

### Likelihood Explanation
The attack requires only: (1) being one of the authorized keys in the target account's `Permission` (which may include any low-weight signer in a real deployment), and (2) computing the trivial malleated variant of one's own valid signature (`s' = N - s`), which needs no additional secret. The transformation is deterministic and can be computed offline. This is reachable directly from any unprivileged transaction/contract-call broadcaster via `TriggerSmartContract` invoking a contract that calls the `ValidateMultiSign` precompile.

### Recommendation
- Enforce canonical low-`s` signatures in `ECDSASignature.validateComponents`/`toCanonicalised` before use in `ValidateMultiSign` (reject `s > HALF_CURVE_ORDER`), consistent with BIP-62-style canonicalization already implemented in `toCanonicalised()` but not invoked here [6](#0-5) .
- Additionally/alternatively, change the `ValidateMultiSign` dedup logic to key strictly off the recovered address (as done in `TransactionCapsule.checkWeight`) rather than off the raw signature bytes, so that any signature recovering to an already-counted address is always skipped regardless of malleated encoding.

### Proof of Concept
1. Deploy/target an account with an Active `Permission` containing keys `K1` (weight 1) and `K2` (weight 1), threshold 2.
2. Attacker controls only `K1`.
3. Attacker signs the message hash `combine(address, permissionId, data)` with `K1`, obtaining `(r, s, v)`.
4. Attacker computes the malleated signature `(r, SECP256K1N - s, v_flipped)` — a pure arithmetic transform, no key needed.
5. Attacker calls a contract that invokes the `ValidateMultiSign` precompile with `signatures = [sig1, sig1_malleated]`.
6. In `ValidateMultiSign.execute`, both signatures recover to `K1`'s address; since their raw bytes differ, `matrixContains(executedSignList, sign)` is false on the second iteration, so `totalWeight` becomes `1 + 1 = 2`, meeting the threshold of 2 with only one real signer, and the precompile returns `true` (`dataOne()`) [7](#0-6) .

### Citations

**File:** crypto/src/main/java/org/tron/common/crypto/ECKey.java (L923-941)
```java
    public static boolean validateComponents(BigInteger r, BigInteger s,
        byte v) {

      if (v != 27 && v != 28) {
        return false;
      }

      if (BIUtil.isLessThan(r, BigInteger.ONE)) {
        return false;
      }
      if (BIUtil.isLessThan(s, BigInteger.ONE)) {
        return false;
      }

      if (!BIUtil.isLessThan(r, SECP256K1N)) {
        return false;
      }
      return BIUtil.isLessThan(s, SECP256K1N);
    }
```

**File:** crypto/src/main/java/org/tron/common/crypto/ECKey.java (L944-946)
```java
    public boolean validateComponents() {
      return validateComponents(r, s, v);
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

**File:** common/src/main/java/org/tron/common/utils/ByteArray.java (L189-196)
```java
  public static boolean matrixContains(List<byte[]> source, byte[] obj) {
    for (byte[] sobj : source) {
      if (Arrays.equals(sobj, obj)) {
        return true;
      }
    }
    return false;
  }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java (L242-267)
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
      if (approveList != null) {
        approveList.add(ByteString.copyFrom(address)); //out put approve list.
      }
      currentWeight += weight;
```
