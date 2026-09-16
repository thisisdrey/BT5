### Title
Multi-signature weight double-counting via ECDSA signature malleability in `ValidateMultiSign` TVM precompile allows bypassing on-chain M-of-N authorization with a single key - ([File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java])

### Summary
The `ValidateMultiSign` precompiled contract, reachable from any TVM smart contract via a normal `TriggerSmartContract` call, is designed to let contracts verify that a set of ECDSA signatures collectively meets a TRON account permission's weight threshold. Its de-duplication logic only skips a signature when the *exact byte-for-byte signature* has already been counted for a signer — it does not reject a **second, distinct, but still cryptographically valid** signature produced by the **same** private key over the same hash. Because ECDSA signatures over secp256k1 are malleable (and/or simply non-deterministic across signing calls), a single key-holder can trivially produce two different valid `(r,s,v)` triples for the same message, causing the same signer's weight to be counted twice and letting one private key satisfy a threshold meant to require two independent co-signers.

### Finding Description
`ValidateMultiSign.execute()` accumulates `totalWeight` over an attacker-supplied signature array: [1](#0-0) 

For each signature it recovers the signer address, then only skips the iteration ("continue") if **both** the recovered address **and** the exact merged `sign` bytes were already seen:

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

`ByteArray.matrixContains` is a plain `Arrays.equals` byte comparison: [2](#0-1) 

If the same signer produces two different valid signatures over the identical `hash` (trivial via ECDSA's random nonce `k`, or via the standard malleability transform `(r, s) -> (r, n-s)` with an adjusted recovery id), `recoveredAddr` is identical on both iterations but the raw signature bytes differ. The code then falls through past the `continue` (only calling `MUtil.checkCPUTime()`, a rate-limit guard, not a rejection) and adds `weight` to `totalWeight` a second time for the same key. This directly contrasts with the equivalent, and correctly fixed, consensus-level function `TransactionCapsule.checkWeight`, which explicitly rejects a signer that "has signed twice" once a hard fork activates, deduping by **recovered address**, not raw signature bytes: [3](#0-2) 

The TVM precompile never received this address-based dedup fix, leaving the double-counting path open. The existing unit test even exercises the "same signer signs twice" scenario but happens to still pass because the test's total signature count already exceeds the threshold with the legitimate second signer included, so the flaw is masked: [4](#0-3) 

### Impact Explanation
`ValidateMultiSign` (and structurally similar `BatchValidateSign`) is intended for smart contracts — e.g., custody/DAO/multisig-wallet contracts deployed on TRON — to gate privileged actions (fund transfers, admin operations) behind an M-of-N approval scheme defined by a TRON account `Permission`. Any holder of a single key with nonzero weight in such a permission can locally forge a second syntactically valid signature over the same authorization hash (no interaction with other co-signers required) and call the consuming contract so that `ValidateMultiSign` reports the threshold satisfied, even though only one real signer approved. This is a direct authorization bypass reachable purely through a `TriggerSmartContract` transaction from an unprivileged account, and it can result in unauthorized approval of fund transfers or privileged operations gated by such contracts — analogous to the Bean token contract's failure to invalidate a signature after use, except here the flaw is "reuse of the same key's authorization under a different but still-valid encoding" rather than exact byte replay.

### Likelihood Explanation
Exploitation requires no privileged access: any account with at least one weighted key in a permission that a target contract relies on can trigger this directly by calling the contract (or by directly invoking the precompile at its fixed address from an attacker's own contract) with two distinct signatures it can generate from its own private key. No signer collusion or additional secrets are needed, and ECDSA signature malleability/non-determinism is trivially controllable by the signer. The main caveat is that the impact depends on downstream production contracts actually using `ValidateMultiSign` for authorization — the java-tron protocol layer itself does not use this precompile for consensus-critical operations, but it is exposed as a supported TVM feature specifically for this multisig-authorization use case.

### Recommendation
Change the de-duplication in `ValidateMultiSign.execute()` (and audit `BatchValidateSign` for the analogous pattern) to reject/skip **any** signature whose `recoveredAddr` has already been counted, regardless of whether the raw signature bytes match — i.e., align with the address-based dedup already used in `TransactionCapsule.checkWeight`:
```
if (ByteArray.matrixContains(executedSignList, recoveredAddr)) {
  continue; // one weight contribution per address, period
}
```
Remove the byte-exact secondary check that allows a differently-encoded signature from the same address to be counted again.

### Proof of Concept
1. Deploy (or use) a TRON account whose `active` permission requires threshold 2 across two keys, `key1` (weight 1) and `key2` (weight 1).
2. As the holder of only `key1`, compute `hash = sha256(address || permissionId || data)` as done in `ValidateMultiSign`.
3. Produce two different valid ECDSA signatures over `hash` using `key1` — either by calling `sign()` twice (nonce `k` differs each call, as shown by the test at `ValidateMultiSignContractTest.java:118-120` producing two different byte sequences for the same key/message) or by taking one signature `(r,s,v)` and deriving the malleable counterpart `(r, n-s, v')`.
4. Call the `ValidateMultiSign` precompile (or a contract that forwards to it) with `signs = [sig1_from_key1, sig2_from_key1]` only — no `key2` signature included.
5. Per the code at `PrecompiledContracts.java:1086-1110`, iteration 1 adds weight 1 for `key1`; iteration 2 recovers the same address but the merged `sign` bytes differ from the first, so the inner `continue` is skipped and weight 1 is added again, yielding `totalWeight = 2 >= threshold`, returning `DATA_FALSE`→`DATA_ONE` (success) despite only one real signer having approved. [5](#0-4)

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

**File:** framework/src/test/java/org/tron/common/runtime/vm/ValidateMultiSignContractTest.java (L102-126)
```java
    //generate data

    byte[] address = key.getAddress();
    int permissionId = 2;
    byte[] data = Sha256Hash.hash(CommonParameter
        .getInstance().isECKeyCryptoEngine(), longData);

    //combine data
    byte[] merged = ByteUtil.merge(address, ByteArray.fromInt(permissionId), data);
    //sha256 of it
    byte[] toSign = Sha256Hash.hash(CommonParameter
        .getInstance().isECKeyCryptoEngine(), merged);

    //sign data

    List<Object> signs = new ArrayList<>();
    signs.add(Hex.toHexString(key1.sign(toSign).toByteArray()));
    //add Repetitive
    signs.add(Hex.toHexString(key1.sign(toSign).toByteArray()));
    signs.add(Hex.toHexString(key2.sign(toSign).toByteArray()));

    Assert.assertArrayEquals(
        validateMultiSign(StringUtil.encode58Check(key.getAddress()), permissionId, data, signs)
            .getValue(), DataWord.ONE().getData());

```
