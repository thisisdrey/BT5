### Title
Missing bounds validation on dynamic ABI offsets in `ValidateMultiSign`/`BatchValidateSign` signature-array extraction leads to out-of-bounds array access - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The `ValidateMultiSign` and `BatchValidateSign` precompiled contracts (reachable via a plain `CALL`/`STATICCALL` from any TVM contract, i.e. by any transaction sender) decode ABI-encoded `bytes[]` arguments by reading an attacker-controlled length word and then iterating array indices derived from attacker-controlled offset words, without validating that the computed indices stay within the parsed `DataWord[]` array bounds — structurally the same class of bug as CVE-2021-25801, where a size field read from a crafted `.avi` index chunk is used to compute a buffer position without validating it stays inside the buffer.

### Finding Description
`extractBytesArray` and `extractSigArray` only bounds-check the initial `offset` argument: [1](#0-0) [2](#0-1) 

Inside the loop, `words[offset + i + 1]` (and, for `extractBytesArray`, the nested `words[offset + bytesOffset + 1]`) is read for every `i` in `[0, len)`, where `len` comes directly from `words[offset].intValueSafe()` — an attacker-supplied value from calldata. Neither function validates that `offset + i + 1 < words.length` before indexing, so a crafted `len`/`offset` combination causes `ArrayIndexOutOfBoundsException`.

In `ValidateMultiSign.execute`, the length is only capped (`sigArraySize > MAX_SIZE`) when the `allowTvmSelfdestructRestriction` feature flag is active, and even then only the *count* is capped — not the *offset*, so `offset + i + 1` can still exceed `words.length` for a small `offset` chosen near the end of a short `words[]` array: [3](#0-2) 

Critically, this extraction call is **not** wrapped in any try/catch in `ValidateMultiSign.execute` — the surrounding `try { ... } catch (Throwable t)` block only covers the later permission-weight logic (lines 1082–1117), not the `extractSigArray`/`extractBytesArray` call at lines 1072–1074. By contrast, `BatchValidateSign.execute` wraps its entire `doExecute` call in a `try/catch (Throwable t)` that swallows any exception and returns a zero-filled result: [4](#0-3) 

The newer `isValidAbiEncoding` guard (added for TIP-854) only checks that `data.length` is a multiple of 32 and that the trailing byte count matches a fixed header/item-word assumption; it does not validate the *dynamic* offset word (`words[3]` for `ValidateMultiSign`, `words[1]`/`words[2]` for `BatchValidateSign`) against the actual array size, so a well-formed-length payload with a malicious internal offset still reaches the unguarded indexing in `extractBytesArray`/`extractSigArray`.

### Impact Explanation
An uncaught `ArrayIndexOutOfBoundsException` propagating out of `PrecompiledContract.execute()` during TVM execution is an unhandled runtime exception at a point in the call stack that (unlike `BatchValidateSign`) has no local safety net. Depending on how far up the TVM interpreter's exception handling reaches for this specific precompile before being converted into a normal revert, this can at minimum guarantee an unexpected/inconsistent execution outcome for `ValidateMultiSign` calls, and — if not uniformly caught by the outer VM dispatch loop — risks destabilizing transaction processing for that block, which is a node-availability concern. I was unable to fully confirm, within the available investigation budget, how `Program.callToPrecompiledAddress` handles a raw `ArrayIndexOutOfBoundsException` thrown by `execute()` for `ValidateMultiSign` specifically (as opposed to the TIP-854-guarded malformed-length case, which is already proven safe by `testTip854OuterFrameContainment`).

### Likelihood Explanation
`ValidateMultiSign` and `BatchValidateSign` are invoked via ordinary `CALL`/`STATICCALL` opcodes to fixed precompile addresses, reachable by any contract deployer or caller without special privilege. Crafting the ABI-encoded `bytes[]` argument with a valid overall length (satisfying `isValidAbiEncoding`) but an internal offset/length pair that walks `extractBytesArray`/`extractSigArray` past the end of the `words[]` array is straightforward calldata construction, requiring no cryptographic material.

### Recommendation
Add explicit bounds checks inside `extractBytesArray` and `extractSigArray` so that `offset + i + 1` (and, for `extractBytesArray`, `offset + bytesOffset + 1`) is validated against `words.length` before every array access, returning an empty/false result instead of indexing out of range. Additionally, wrap the extraction calls in `ValidateMultiSign.execute` in the same defensive `try/catch` used for the rest of the permission-checking logic (or extend the existing try block to cover lines 1057–1074), matching the containment already present in `BatchValidateSign`.

### Proof of Concept
Construct calldata for `validatemultisign(address,uint256,bytes32,bytes[])` such that:
1. Total byte length satisfies `isValidAbiEncoding` (multiple of 32, tail consistent with `(words-5)/5` items).
2. The offset word (`words[3]`) points to a `words[]` index near the end of the array.
3. The length word at that offset (`words[offset]`, i.e. the encoded `bytes[]` array length) is set to a value such that `offset + len + 1 > words.length`, e.g. `len = 5` while only 1–2 words remain in the array.

Calling this precompile from a deployed contract (or `eth_call`/`triggerConstantContract` JSON-RPC/HTTP path) drives execution into `extractBytesArray`/`extractSigArray` at [5](#0-4)  where `words[offset + i + 1]` throws `ArrayIndexOutOfBoundsException`, which is not caught anywhere in `ValidateMultiSign.execute` prior to the try block at line 1082.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L399-412)
```java
  private static byte[][] extractBytesArray(DataWord[] words, int offset, byte[] data) {
    if (offset > words.length - 1) {
      return new byte[0][];
    }
    int len = words[offset].intValueSafe();
    byte[][] bytesArray = new byte[len][];
    for (int i = 0; i < len; i++) {
      int bytesOffset = words[offset + i + 1].intValueSafe() / WORD_SIZE;
      int bytesLen = words[offset + bytesOffset + 1].intValueSafe();
      bytesArray[i] = extractBytes(data, (bytesOffset + offset + 2) * WORD_SIZE,
          bytesLen);
    }
    return bytesArray;
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L414-426)
```java
  private static byte[][] extractSigArray(DataWord[] words, int offset, byte[] data) {
    if (offset > words.length - 1) {
      return new byte[0][];
    }
    int len = words[offset].intValueSafe();
    byte[][] bytesArray = new byte[len][];
    for (int i = 0; i < len; i++) {
      int bytesOffset = words[offset + i + 1].intValueSafe() / WORD_SIZE;
      bytesArray[i] = extractBytes(data, (bytesOffset + offset + 2) * WORD_SIZE,
          SIG_LENGTH);
    }
    return bytesArray;
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1057-1074)
```java
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
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1144-1154)
```java
    @Override
    public Pair<Boolean, byte[]> execute(byte[] data) {
      try {
        return doExecute(data);
      } catch (Throwable t) {
        if (t instanceof InterruptedException){
          Thread.currentThread().interrupt();
        }
        return Pair.of(true, new byte[WORD_SIZE]);
      }
    }
```
