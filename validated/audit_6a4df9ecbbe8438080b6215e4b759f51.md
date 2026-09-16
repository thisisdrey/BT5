Based on my investigation, I found a directly analogous bug class in `PrecompiledContracts.java`.

### Title
Unbounded attacker-controlled length in `extractBytesArray`/`extractSigArray` causes uncaught `ArrayIndexOutOfBoundsException` in TVM precompile calldata parsing - ([File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java])

### Summary
The `ValidateMultiSign` and `BatchValidateSign` TVM precompiled contracts parse ABI calldata into a fixed-size `DataWord[]` array sized to the caller-supplied `data.length`, then use an attacker-controlled length word to drive a loop that indexes further into that same array without checking the length against the array bounds — the same root cause pattern as the reported rtl8723bs bug: a length field taken from untrusted input is used to advance/index a loop with only a partial or missing bounds check.

### Finding Description
`extractBytesArray` and `extractSigArray` read `len = words[offset].intValueSafe()` and then loop `for (int i = 0; i < len; i++)`, indexing `words[offset + i + 1]` and (for `extractBytesArray`) also `words[offset + bytesOffset + 1]`, with no check that `offset + i + 1` (or the nested `bytesOffset`) stays within `words.length`: [1](#0-0) 

`words` comes from `DataWord.parseArray(rawData)`, which slices the raw calldata into fixed 32-byte words — its length is strictly determined by `rawData.length / 32`, not by any declared array length inside the payload. Both `ValidateMultiSign.execute` and `BatchValidateSign.doExecute` call `words[3]`/`words[1]` (attacker-controlled offsets) to locate the array header, then call `extractSigArray`/`extractBytesArray` with a `len` value read directly from calldata: [2](#0-1) [3](#0-2) 

Both call sites invoke `extractSigArray`/`extractBytesArray` **before** entering the `try { ... } catch (Throwable t)` block that wraps the actual signature-recovery logic in `ValidateMultiSign` (the try/catch starts only around account/permission processing), and `BatchValidateSign.execute` wraps `doExecute` in `try { ... } catch (Throwable t)` which does catch it, but `ValidateMultiSign.execute` has no equivalent wrapper around the `extractBytesArray`/`extractSigArray` calls at lines 1072-1074, so a crafted `len` that exceeds `words.length` throws an uncaught `ArrayIndexOutOfBoundsException` out of `execute()`.

There is a partial guard (`allowTvmSelfdestructRestriction` limits `sigArraySize > MAX_SIZE`), but that guard only applies when the feature flag is enabled and does not validate that `offset + len` stays inside `words.length`; when the flag is disabled, or when `len` is used to index past the bound before the `MAX_SIZE` check is meaningfully applied, the array-index guard is missing entirely — mirroring the missing "IE length extends past len" bound check in the original CVE.

### Impact Explanation
An `ArrayIndexOutOfBoundsException` thrown directly from `PrecompiledContracts.ValidateMultiSign.execute()` is a `RuntimeException` that is not caught locally. Depending on how far up the TVM call stack it propagates before being caught by a generic handler (e.g., in `VM.play()`'s `catch (RuntimeException e)`), this is likely to manifest as a deterministic revert of the calling transaction rather than node crash, since Java exceptions unwind consistently on all full nodes. I was not able to fully trace the precompiled-contract invocation site (`Program.java`'s call-to-precompile dispatch) within the remaining budget to confirm whether any node-specific state or resource leak occurs before the exception is caught, so this should be validated further before treating it as more than a per-call revert / minor consensus-non-impacting exception path.

### Likelihood Explanation
This precompile is reachable by any contract call via `TRIGGERSMARTCONTRACT`/internal `CALL` addressed at the `ValidateMultiSign`/`BatchValidateSign` precompile addresses, requiring only crafted calldata — no special privileges. Constructing calldata where the declared array length exceeds the actual number of 32-byte words is trivial.

### Recommendation
Add explicit bounds checks in `extractBytesArray`, `extractSigArray`, and `extractBytes32Array` verifying `offset + len + 1 <= words.length` (and that any nested `bytesOffset` computed from calldata also stays within bounds) before looping, returning an empty/failure result instead of throwing when the declared length would read past the parsed `words` array — analogous to the two guards added in the upstream fix (header-fits check and data-window-fits check).

### Proof of Concept
Not able to fully construct and verify an end-to-end PoC transaction within available tool budget (would require confirming whether the exception is caught before reaching consensus-critical state). Conceptually: craft calldata to `ValidateMultiSign` (`address`, `permissionId`, `data`, `offset` pointing near the end of calldata, and a length word at `words[offset]` set to a large value like `0xFFFFFFFF`); `extractSigArray`/`extractBytesArray` will then index `words[offset + i + 1]` far beyond `words.length`, throwing `ArrayIndexOutOfBoundsException` before the `try/catch` in `ValidateMultiSign.execute()` is entered. [4](#0-3)

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L390-430)
```java
  private static byte[][] extractBytes32Array(DataWord[] words, int offset) {
    int len = words[offset].intValueSafe();
    byte[][] bytes32Array = new byte[len][];
    for (int i = 0; i < len; i++) {
      bytes32Array[i] = words[offset + i + 1].getData();
    }
    return bytes32Array;
  }

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

  private static byte[] extractBytes(byte[] data, int offset, int len) {
    return Arrays.copyOfRange(data, offset, offset + len);
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1051-1074)
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
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1156-1177)
```java
    private Pair<Boolean, byte[]> doExecute(byte[] data)
        throws InterruptedException, ExecutionException {
      if (VMConfig.allowTvmOsaka()
          && !isValidAbiEncoding(data, ABI_HEADER_WORDS, ABI_ITEM_WORDS)) {
        return Pair.of(false, EMPTY_BYTE_ARRAY);
      }
      DataWord[] words = DataWord.parseArray(data);
      byte[] hash = words[0].getData();

      if (VMConfig.allowTvmSelfdestructRestriction()) {
        int sigArraySize = words[words[1].intValueSafe() / WORD_SIZE].intValueSafe();
        int addrArraySize = words[words[2].intValueSafe() / WORD_SIZE].intValueSafe();
        if (sigArraySize > MAX_SIZE || addrArraySize > MAX_SIZE) {
          return Pair.of(true, DATA_FALSE);
        }
      }

      byte[][] signatures = VMConfig.allowTvmSelfdestructRestriction() ?
          extractSigArray(words, words[1].intValueSafe() / WORD_SIZE, data) :
          extractBytesArray(words, words[1].intValueSafe() / WORD_SIZE, data);
      byte[][] addresses = extractBytes32Array(
          words, words[2].intValueSafe() / WORD_SIZE);
```
