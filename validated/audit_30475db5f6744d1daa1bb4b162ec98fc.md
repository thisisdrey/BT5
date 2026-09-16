### Title
Multi-signature weight double-counting via signature malleability in `ValidateMultiSign` precompile - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The `ValidateMultiSign` TVM precompiled contract accumulates permission weight for each supplied signature, but its duplicate-detection logic keys on the raw signature bytes rather than on the recovered signer address. Because ECDSA signatures are malleable (multiple distinct `(r, s, v)` byte encodings can recover to the same address for the same message hash, and the `sign` bytes are used directly as part of the de-dup key), a caller can submit two syntactically different signatures that recover to the *same* address and have that single signer's weight counted twice, bypassing the intended multisig threshold check.

### Finding Description
`recoverAddrBySign` derives the signer address from raw signature bytes without normalizing/canonicalizing the signature (e.g. no low-S enforcement), consistent with `ECDSASignature.validateComponents`/`Rsv.fromSignature` which merely bound `r`/`s` within `[1, SECP256K1N)` [1](#0-0) , [2](#0-1) .

In `ValidateMultiSign.execute`, for each signature the code recovers the address, merges `(recoveredAddr, sign)` into a marker, and checks whether that *specific address* was already processed. If the address was seen before but the merged `(address, sign)` marker was **not** seen (i.e., a different raw signature encoding for the same address), the code does *not* skip the entry — it falls through and adds `weight` to `totalWeight` again: [3](#0-2) 

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

The intent of the `matrixContains(executedSignList, recoveredAddr)` check is clearly to prevent the same signer from being counted more than once toward `totalWeight`. However, the inner check only `continue`s (skips re-adding weight) when the *exact byte-for-byte signature* was already seen — it treats "same address, different signature bytes" as a *new* contribution rather than a duplicate. Because ECDSA allows more than one valid `(r,s,v)` for a given signer/message (malleability — e.g. `(r, s, v)` vs `(r, N-s, v')`), an attacker holding a single valid signature can derive a second, syntactically distinct but still-valid signature for the same address/message and submit both. Both entries pass `signature.validateComponents()` and `recoverAddrBySign`, both add `weight` for the same address, and `totalWeight` is inflated using only one real private key.

### Impact Explanation
This allows a single co-owner of a multi-signature TRON account (one whose weight alone is below `permission.getThreshold()`) to satisfy the threshold check on their own by supplying two malleable variants of their own signature, causing `ValidateMultiSign` to return "true" (`dataOne()`) as if independent signers had approved. Any smart contract that relies on this precompile to gate privileged operations (fund transfers, permission changes, contract calls) can be tricked into executing an operation that should have required multiple independent co-signers — i.e., unauthorized account operation / theft of funds gated behind a multisig threshold enforced via this precompile [4](#0-3) .

### Likelihood Explanation
Reachable by any contract deployer/caller: `ValidateMultiSign` is a standard TVM precompiled contract invoked via a normal contract call (`execute(byte[] rawData)`), requiring only a permission ID, target address, data, and an array of signatures — all attacker-controlled inputs from a broadcast transaction [5](#0-4) . Producing a second malleable signature for an already-known valid `(r,s)` pair is a standard, well-known ECDSA property (flipping `s` to `N-s` and the recovery id) and requires no additional secret knowledge beyond the one valid signature the attacker already possesses.

### Recommendation
Change the duplicate check to be keyed solely on `recoveredAddr` (drop or ignore the raw signature bytes for de-duplication): once an address has contributed weight, any further signature recovering to that same address must be skipped entirely rather than only skipped when byte-identical. Additionally, canonicalize signatures (enforce low-S, as EIP-2 / BIP-62 do) before use so that only one canonical encoding is accepted per signer/message.

### Proof of Concept
1. Deploy/target an account with an `Active` permission requiring `threshold = 2`, where signer `A` alone has `weight = 1` (insufficient alone).
2. Off-chain, `A` signs the `(address, permissionId, data)` hash once, obtaining `(r, s, v)`.
3. Derive the malleable counterpart `(r, N-s, v')` for the same key/message (standard ECDSA transform), obtaining a second syntactically different but validly-recovering signature for the same address `A`.
4. Call the `ValidateMultiSign` precompile (address `0x0a`) with `signatures = [sig1, sig2]`.
5. In `execute`, `sig1` recovers to `A`, adds `weight=1`; `sig2` also recovers to `A`, and because `merge(A, sig2) != merge(A, sig1)`, the `matrixContains(executedSignList, sign)` check fails to match, so the code falls through and adds `weight=1` again, reaching `totalWeight = 2 >= threshold`, returning `dataOne()` (true) despite only one real co-signer having participated [6](#0-5) .

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1051-1080)
```java
    @Override
    public Pair<Boolean, byte[]> execute(byte[] rawData) {
      if (VMConfig.allowTvmOsaka()
          && !isValidAbiEncoding(rawData, ABI_HEADER_WORDS, ABI_ITEM_WORDS)) {
        return Pair.of(false, EMPTY_BYTE_ARRAY);
      }
      DataWord[] words = DataWord.parseArray(rawData);
      byte[] address = words[0].toTronAddress();
      int permissionId = words[1].intValueSafe();
      byte[] data = words[2].getData();

      byte[] combine = ByteUtil.merge(address, ByteArray.fromInt(permissionId), data);
      byte[] hash = Sha256Hash.hash(CommonParameter
          .getInstance().isECKeyCryptoEngine(), combine);

      if (VMConfig.allowTvmSelfdestructRestriction()) {
        int sigArraySize = words[words[3].intValueSafe() / WORD_SIZE].intValueSafe();
        if (sigArraySize > MAX_SIZE) {
          return Pair.of(true, DATA_FALSE);
        }
      }
      byte[][] signatures = VMConfig.allowTvmSelfdestructRestriction() ?
          extractSigArray(words, words[3].intValueSafe() / WORD_SIZE, rawData) :
          extractBytesArray(words, words[3].intValueSafe() / WORD_SIZE, rawData);

      if (signatures.length == 0 || signatures.length > MAX_SIZE) {
        return Pair.of(true, DATA_FALSE);
      }

      AccountCapsule account = this.getDeposit().getAccount(address);
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
