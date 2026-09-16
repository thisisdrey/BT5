### Title
Multi-sig weight can be double-counted via padded signatures in `ValidateMultiSign` precompile - ([File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java])

### Summary
The `recoverAddrBySign` helper used by the `ValidateMultiSign` TVM precompile accepts any signature of length `>= 65` bytes instead of requiring an exact length of `65`. This is the same bug class as the ENS `equals()` finding: a length check that should be `==` is instead `>=`/`<`, so trailing garbage bytes are silently ignored during signature parsing but still make the byte array *distinct* for downstream duplicate-detection logic that compares raw signature bytes.

### Finding Description
`recoverAddrBySign` only rejects signatures shorter than 65 bytes: [1](#0-0) 

```java
private static byte[] recoverAddrBySign(byte[] sign, byte[] hash) {
    byte[] out = null;
    if (ArrayUtils.isEmpty(sign) || sign.length < 65) {
      return new byte[0];
    }
    ...
    Rsv rsv = Rsv.fromSignature(sign);
    ...
}
```
`Rsv.fromSignature` only consumes the first 65 bytes (r, s, v), so any bytes appended beyond index 65 are ignored for the purpose of ECDSA recovery — the same recovered address is produced for `sign` and for `sign || <arbitrary padding>`.

In `ValidateMultiSign.execute`, this recovered address and the *raw* signature bytes are used together to prevent a signer from being counted twice: [2](#0-1) 

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

The de-duplication only skips a signature when the exact combined `recoveredAddr + sign` byte array is already present in `executedSignList`. Because `recoverAddrBySign` tolerates any signature `length >= 65`, an attacker can submit the *same underlying valid signature* multiple times with different trailing padding appended (e.g. `sig || 0x00`, `sig || 0x01`, ...). Each padded variant:
- Recovers to the identical `recoveredAddr` (since only the first 65 bytes are used by `Rsv.fromSignature`).
- Produces a different `sign` (merged) byte array because the raw bytes differ.

As a result, `ByteArray.matrixContains(executedSignList, sign)` is `false` for every padded variant, so the "already contains sign" `continue` is never taken, and `weight = TransactionCapsule.getWeight(permission, recoveredAddr)` is added to `totalWeight` again for every padded variant of the same key. The `MUtil.checkCPUTime()` call is only a CPU/time throttling guard, not a security control that blocks double counting.

`words[3]` bounds the number of signature entries (`MAX_SIZE = 5`, or up to `MAX_SIZE` after `allowTvmSelfdestructRestriction`), but nothing prevents those few entries from all being padded variants of one signature.

### Impact Explanation
This precompile is directly reachable by any TVM contract call and is used to validate TRON account multi-signature permissions from within smart contracts (`validatemultisign(address,uint256,bytes32,bytes[])`). By supplying several padded copies of a single valid signature from one key holder, an attacker who controls only one key of a multi-key permission can inflate `totalWeight` past `permission.getThreshold()`, causing the precompile to report success (`DATA_FALSE` → success `1`) even though the real signer weight is insufficient. Any smart contract logic gating a state-changing/fund-moving action on this precompile's result can be bypassed by a party who does not actually hold enough keys/weight to satisfy the configured account permission — an unauthorized-operation / permission-bypass condition.

### Likelihood Explanation
Exploitation only requires:
- A TRON account with a multi-sig `Permission` whose threshold requires more than one key's weight.
- Access to exactly one valid signature over the hash (which the attacker, as one of the keyholders, legitimately has).
- Calling the `ValidateMultiSign` precompile (address `0x...a`) with a `bytes[]` array containing several padded copies of that one signature.

No special privilege, timing, or race condition is required; the vulnerable code path is on the primary execution path of the precompile and is reachable from any contract call, making this Medium-High likelihood for an attacker who legitimately holds a subset of keys.

### Recommendation
Require signatures to be exactly 65 bytes (or an exact expected length, matching how `ECRecover`/other precompiles validate raw signature length) before recovery, e.g.:
```java
if (ArrayUtils.isEmpty(sign) || sign.length != 65) {
  return new byte[0];
}
```
Additionally, consider basing duplicate detection solely on `recoveredAddr` (one weight contribution per unique address per call) rather than on the raw signature bytes, since the current design relies on byte-exact signature comparison as a proxy for "same signer," which is unsound whenever signature parsing is lenient about extra bytes.

### Proof of Concept
1. Deploy/identify an account with an Active permission requiring 2 keys (threshold = 2), each with weight 1 (`key1`, `key2`).
2. Attacker controls only `key1`.
3. Attacker computes the canonical hash `combine = address || permissionId || data`, `hash = sha256(combine)`, and produces one valid 65-byte ECDSA signature `sig` with `key1`.
4. Attacker builds `signatures = [sig, sig || 0x00]` (two entries: the original signature, and the same signature with one extra trailing byte).
5. Calls `ValidateMultiSign` precompile with these two signature entries.
6. In `execute`: first iteration recovers `key1`'s address, adds weight 1, records `sign1 = addr||sig`. Second iteration recovers the same `key1` address (since `Rsv.fromSignature` ignores the trailing byte), builds `sign2 = addr||sig||0x00` which is *not* found in `executedSignList` (`matrixContains` fails), so it is **not skipped**; weight 1 is added again — `totalWeight = 2 >= threshold(2)` — the precompile returns success even though only `key1` ever signed. [3](#0-2)

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
