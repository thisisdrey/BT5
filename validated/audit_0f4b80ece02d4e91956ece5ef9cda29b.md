### Title
Signer Weight Double-Counting via Non-Unique Signature Encodings in `ValidateMultiSign` Precompile - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The `ValidateMultiSign` precompiled contract (invoked by any TVM contract to check multisig permission thresholds) deduplicates recovered signers by the *raw signature bytes* rather than by the *recovered address*. Because ECDSA/ECRecover based signatures for the same message and same private key are not unique (multiple distinct `(r,s,v)` triples recover to the same address — the exact "signature malleability / non-uniqueness" class flagged in the source report), a caller controlling a single private key can submit several distinct-but-valid signatures for the same signer and have that signer's weight counted multiple times toward the permission threshold.

### Finding Description
`ValidateMultiSign.execute` iterates the supplied signature array, recovers an address for each entry via `recoverAddrBySign`, and only skips weight accumulation when the *exact byte-identical* `sign` (address‖signature) has already been recorded: [1](#0-0) 

Specifically:
```
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
```
When `recoveredAddr` is already present but the new `sign` bytes differ, the code falls through to `MUtil.checkCPUTime()` (an anti-DoS throttle only) and then **still adds `weight` again** instead of skipping the entry. The signature-verification primitive underlying `recoverAddrBySign` uses raw `ecrecover`-style recovery via `SignUtils.signatureToAddress` / `ECKey.recoverPubBytesFromSignature`, which is inherently non-unique per message/key (classic ECDSA malleability: `(r, s)` and `(r, n-s)` both recover to the same address; independently, a fresh nonce `k` yields yet another valid, distinct `(r,s)` for the same key and message): [2](#0-1) 

For contrast, java-tron's own consensus-layer multisig check (`TransactionCapsule.checkWeight`) was hardened after fork `VERSION_4_7_1` to key its dedup map on the *recovered address* rather than the raw signature, precisely to close this class of double-count bug: [3](#0-2) 

The `ValidateMultiSign` TVM precompile never received the equivalent fix — it still allows a single key's weight to be counted once per distinct signature encoding, up to `MAX_SIZE` (5) signatures: [4](#0-3) 

### Impact Explanation
`ValidateMultiSign` is a public precompiled contract (address callable from any deployed TVM contract) used by multisig-gated smart contracts to authorize privileged operations (fund transfers, permission-gated actions) based on a weighted threshold of an account's `Permission` keys. By supplying up to `MAX_SIZE` (5) distinct valid signatures from a single controlled key (trivially producible — either via ECDSA malleability `(r, n-s)` or simply by signing the same hash again with a different nonce), an attacker can inflate `totalWeight` up to 5x the weight of one key. This allows an attacker holding a single low-weight key to satisfy a multisig `threshold` that should require multiple independent signers, resulting in unauthorized approval/execution of privileged contract logic protected by this precompile — a concrete unauthorized-account-operation / theft-of-funds primitive.

### Likelihood Explanation
Reachable directly by any unprivileged transaction sender: deploy or call any TVM contract that invokes the `ValidateMultiSign` precompile, supplying a crafted `bytes[]` signature array containing multiple distinct valid signatures from the same key. No special privileges, node cooperation, or SR/witness involvement are required — only ordinary transaction broadcasting and a single private key already present with nonzero weight in the target account's permission.

### Recommendation
In `ValidateMultiSign.execute`, deduplicate strictly by `recoveredAddr` (skip weight accumulation whenever the address has already been counted, regardless of the specific signature bytes), matching the fix already applied in `TransactionCapsule.checkWeight` post `VERSION_4_7_1`. Additionally, consider enforcing canonical (low-s) signature form during recovery to reduce the number of accepted encodings per key, consistent with the original report's recommendation to avoid raw, malleable `ecrecover` usage.

### Proof of Concept
1. Attacker controls key `K` with weight `w` inside an account `Permission` (threshold `T > w`, other unrelated keys unknown/uncontrolled by attacker).
2. Attacker computes `hash = sha256(address ‖ permissionId ‖ data)` per `ValidateMultiSign` logic (lines 1058-1064).
3. Attacker signs `hash` twice with key `K`, producing two distinct valid `(r1,s1,v1)` and `(r2,s2,v2)` signatures that both recover to `K`'s address (achievable via ECDSA malleability `s2 = n - s1`, or simply two independent signing operations).
4. Attacker calls the contract passing `signatures = [sig1, sig2]` (and can repeat up to `MAX_SIZE=5`).
5. In the loop, `sig1` recovers `K`, `totalWeight += w`. `sig2` recovers `K` again; since `sig2` bytes differ from `sig1`, the `ByteArray.matrixContains(executedSignList, sign)` check fails to short-circuit, and `totalWeight += w` again, yielding `totalWeight = 2w`.
6. If `2w >= T`, the precompile returns success (`dataOne()`), bypassing the intended multisig threshold with only one real signer. [5](#0-4)

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1036-1049)
```java
  public static class ValidateMultiSign extends PrecompiledContract {

    private static final int ENGERYPERSIGN = 1500;
    private static final int MAX_SIZE = 5;
    private static final int ABI_HEADER_WORDS = 5;
    private static final int ABI_ITEM_WORDS = 5;


    @Override
    public long getEnergyForData(byte[] data) {
      long cnt = (data.length / WORD_SIZE - 5) / 5;
      // one sign 1500, half of ecrecover
      return cnt * ENGERYPERSIGN;
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

**File:** chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java (L233-270)
```java
  public static long checkWeight(Permission permission, List<ByteString> sigs, byte[] hash,
      List<ByteString> approveList)
      throws SignatureException, PermissionException, SignatureFormatException {
    long currentWeight = 0;
    if (sigs.size() > permission.getKeysCount()) {
      throw new PermissionException(
          "Signature count is " + (sigs.size()) + " more than key counts of permission : "
              + permission.getKeysCount());
    }
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
    }
    return currentWeight;
  }
```
