### Title
ECDSA Signature Malleability Allows Duplicate-Signer Weight Counting in `ValidateMultiSign` Precompile - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The `ValidateMultiSign` TVM precompiled contract (address `0x66`) recovers a signer address for each supplied ECDSA signature and accumulates permission "weight" toward a multisig threshold. Because the recovered-address check only skips a signature when the *exact byte string* has been seen before, an attacker who possesses a single valid ECDSA signature for a hash can derive a second, differently-encoded signature (via ECDSA's well-known `s → n-s`, `v` flip malleability) that recovers to the *same* address, yet is treated as a new, distinct approval. This lets a single real signer's weight be counted twice (or more), letting an attacker satisfy a multisig `threshold` that was supposed to require independent approvals from multiple distinct keys.

### Finding Description
`PrecompiledContracts.ValidateMultiSign.execute()`: [1](#0-0) 

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
    return Pair.of(true, DATA_FALSE);
  }
  totalWeight += weight;
  executedSignList.add(sign);
  executedSignList.add(recoveredAddr);
}
```

The de-duplication logic is keyed on the *combined* `(recoveredAddr, rawSignatureBytes)` pair, not on `recoveredAddr` alone. If `recoveredAddr` is already present but the specific `sign` bytes are not, execution does **not** `continue`; it merely calls `MUtil.checkCPUTime()` (an energy/CPU throttle) and then falls through to add `weight` again to `totalWeight`. Because ECDSA signatures are malleable — for any valid `(r, s, v)` there exists a second valid signature `(r, n-s, v')` for the same message hash that recovers to the identical address — an attacker holding one legitimate signature from a key can trivially compute this second byte-distinct signature and submit both. The loop will treat them as two independent approvals from (what appears to be) the same key but two "different" signature blobs, doubling the counted weight without needing a second private key.

Contrast with the correct pattern used elsewhere in the codebase, `TransactionCapsule.checkWeight`, which deduplicates purely on the *recovered address* (via `addMap.containsKey(base64)` keyed by the encoded address) and explicitly throws `"has signed twice!"` — this is the safe design that `ValidateMultiSign` fails to replicate: [2](#0-1) 

Additionally, the underlying `ECDSASignature.validateComponents` never rejects the high-`s` (non-canonical) form, so malleated signatures pass signature-format validation: [3](#0-2) 

### Impact Explanation
`ValidateMultiSign` is a public TVM precompile invocable by any smart contract, and is the standard mechanism TRON smart-contract wallets/DApps use to verify on-chain multisig approval (an alternative to `AccountPermissionUpdateContract` k-of-n gating implemented in Solidity). Any deployed contract relying on this precompile to gate privileged operations (fund transfers, governance actions, upgrade approvals) behind an N-of-M weight threshold can be tricked into approving an operation with fewer independent approvals than intended, because a single compromised or colluding key's one signature can be split into multiple "distinct" signature bytes that each count separately toward the weight sum. This is a direct "unauthorized account operation" / theft-of-funds vector for any DApp trusting this precompile's threshold result, which matches the CVE's underlying bug class (broken cryptographic handling that undermines an authorization/identity check).

### Likelihood Explanation
Exploitation only requires: (1) one legitimate ECDSA signature over the hash the contract computes (`sha256(address || permissionId || data)`), and (2) trivial big-integer arithmetic (`s' = n - s`, flip parity bit) to produce a second syntactically valid, differently-encoded signature recovering to the same address — a well-known, decades-old ECDSA property requiring no cryptographic breakthrough. Any contract or bytecode caller can invoke the precompile directly with crafted `bytes[]` signature arrays, making this reachable by an unprivileged contract deployer/caller with no special permission on-chain.

### Recommendation
Change the de-duplication key in `ValidateMultiSign.execute()` (and audit `BatchValidateSign` for the same pattern) to dedupe strictly on `recoveredAddr`, mirroring `TransactionCapsule.checkWeight`: once an address has contributed weight, any further signature recovering to that same address must be skipped (`continue`), regardless of whether the raw signature bytes differ. Additionally, consider enforcing canonical low-`s` signatures in `ECDSASignature.validateComponents`/`recoverAddrBySign` to eliminate the malleable variant altogether.

### Proof of Concept
1. Deploy/target a contract that calls the `ValidateMultiSign(address, permissionId, data, bytes[] signatures)` precompile with an account permission `threshold = 2`, and two keys `K1` (weight 1) and `K2` (weight 1).
2. Obtain one legitimate signature `sig1 = K1.sign(hash)` (e.g., leaked, phished, or the attacker is `K1` itself but should only count once).
3. Compute a malleated variant `sig1' = (r, n-s, flipped v)` from `sig1` — this is standard ECDSA math, no private key needed.
4. Call the precompile with `signatures = [sig1, sig1']`. Both recover to `K1`'s address. Because `sig1 != sig1'` as raw bytes, the `matrixContains(executedSignList, sign)` check for the second entry is false, so weight is added twice (`totalWeight = 2`), meeting `threshold = 2` even though only `K1` ever signed.
5. The precompile returns `dataOne()` (success), and any contract logic gating a fund transfer or privileged action on this result proceeds despite only one real signer having approved. [4](#0-3)

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1036-1121)
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
        } catch (Throwable t) {
          if (t instanceof OutOfTimeException) {
            throw t;
          }
          logger.info("ValidateMultiSign error:{}", t.getMessage());
        }
      }
      return Pair.of(true, DATA_FALSE);
    }
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
