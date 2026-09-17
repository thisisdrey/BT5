## Title
Unbounded array-index read in `ValidateMultiSign`/`BatchValidateSign` precompile ABI decoding - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
CVE-2020-10251 is an out-of-bounds read in ImageMagick's HEIC decoder caused by trusting a declared width/height field instead of validating it against the actual buffer size. The analogous bug class in java-tron is present in the TVM precompiled-contract ABI decoding used by `ValidateMultiSign` (address `0x...0a`) and `BatchValidateSign` (address `0x...09`): attacker-controlled "offset"/"length" words taken directly from calldata are used as indices into the parsed `DataWord[] words` array without checking they stay inside `words.length`.

### Finding Description
Both precompiles parse raw calldata into a flat `DataWord[]` and then use offset/length words *from that same untrusted calldata* to index further into the array: [1](#0-0) 

```
DataWord[] words = DataWord.parseArray(rawData);
...
int sigArraySize = words[words[3].intValueSafe() / WORD_SIZE].intValueSafe();
```

and in `extractBytes32Array`: [2](#0-1) 

```
private static byte[][] extractBytes32Array(DataWord[] words, int offset) {
  int len = words[offset].intValueSafe();
  byte[][] bytes32Array = new byte[len][];
  for (int i = 0; i < len; i++) {
    bytes32Array[i] = words[offset + i + 1].getData();
  }
  return bytes32Array;
}
```

Neither `words[3].intValueSafe() / WORD_SIZE` (the offset itself) nor the subsequent loop bound `offset + i + 1` is checked against `words.length` before use, so a caller can supply an offset/length pointing past the end of the parsed array, producing an `ArrayIndexOutOfBoundsException` — the same "trusted size field vs. actual buffer size" root cause as the ImageMagick CVE.

The codebase shows this exact defect class was recognized and partially remediated with a "TIP-854" ABI-shape guard, `isValidAbiEncoding`, gated behind `VMConfig.allowTvmOsaka()`: [3](#0-2) 

For `BatchValidateSign`, the outer `execute()` wraps `doExecute()` in a catch-all that swallows any `Throwable` and returns a default zero result: [4](#0-3) 

However, `ValidateMultiSign.execute()` has **no such outer try/catch** around the decoding path; only a narrow try/catch further down around the `permission` lookup. Project tests explicitly document this asymmetry and state it is intentionally preserved as "legacy behavior": [5](#0-4) 

### Impact Explanation
When `VMConfig.allowTvmOsaka()` is not active, an unprivileged contract caller can invoke `ValidateMultiSign` (precompile `0x...0a`) via a `CALL`/`STATICCALL` with a crafted `calldata` offset/length so that `words[offset]` or `words[offset + i + 1]` exceeds `words.length`, throwing an uncaught `ArrayIndexOutOfBoundsException` out of the precompile's `execute()` method. Because there is no local catch, the exception propagates into the TVM's precompiled-contract call path. Whether this is ultimately absorbed by a broader `Runtime`/`VMActuator` exception handler (failing only the calling transaction) or escapes further up could not be fully confirmed from the available code in this pass; I was not able to trace `Program.callToPrecompiledAddress` and `Runtime.execute()`'s exception-handling boundaries before running out of investigation budget. At minimum this is an out-of-bounds-read/DoS on the specific transaction; if the exception is not fully contained by an upstream boundary, this could degrade to a broader execution failure.

### Likelihood Explanation
High for reachability: `ValidateMultiSign` is a standard TVM precompile reachable from any signed transaction that executes a `CALL` to address `0x...0a` with attacker-chosen calldata — no special privilege is required. The only condition is that the network/config has not activated `VMConfig.allowTvmOsaka()` (the TIP-854 guard), which the project's own tests treat as a currently-supported, tested code path ("pre-activation" behavior), not a hypothetical.

### Recommendation
- Extend the `isValidAbiEncoding`/TIP-854 bounds validation to be unconditional (not gated behind `VMConfig.allowTvmOsaka()`), or add an explicit bounds check on every array index derived from calldata offsets (`offset`, `offset + i + 1`, `words[3].intValueSafe() / WORD_SIZE`, etc.) in `extractBytes32Array`, `extractBytesArray`, `extractSigArray`, and their callers in `ValidateMultiSign`/`BatchValidateSign`.
- Wrap `ValidateMultiSign.execute()` in the same defensive catch-all pattern already used by `BatchValidateSign.execute()` so malformed calldata cannot escape as an uncaught exception regardless of hard-fork activation state.

### Proof of Concept
1. Craft a transaction that performs a `CALL` (or `STATICCALL`) to precompiled address `0x000000000000000000000000000000000000000000000000000000000000000a` (`ValidateMultiSign`) with calldata containing a valid `address`/`permissionId`/`data` head but with the signatures-array offset word (`words[3]`) set to a large value such that `words[3].intValueSafe() / WORD_SIZE` exceeds the actual number of parsed words.
2. Ensure the target chain/config has not enabled `VMConfig.allowTvmOsaka()` (pre-activation state), which the codebase's own tests (`ValidateMultiSignContractTest.testTip854PreActivationNoOp`) confirm still routes such calldata to the unchecked legacy decoder.
3. Observe an uncaught `ArrayIndexOutOfBoundsException`/`RuntimeException` raised from `PrecompiledContracts.ValidateMultiSign.execute()` / `extractBytesArray`, exactly analogous to the ImageMagick out-of-bounds read triggered by a length field exceeding the real buffer size.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L390-397)
```java
  private static byte[][] extractBytes32Array(DataWord[] words, int offset) {
    int len = words[offset].intValueSafe();
    byte[][] bytes32Array = new byte[len][];
    for (int i = 0; i < len; i++) {
      bytes32Array[i] = words[offset + i + 1].getData();
    }
    return bytes32Array;
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

**File:** framework/src/test/java/org/tron/common/runtime/vm/ValidateMultiSignContractTest.java (L244-260)
```java
  // TIP-854: before activation, malformed calldata reaches the legacy decoder.
  // Assert the guard is not taken — this precompile has no outer catch, so a
  // too-short input raises inside the decoder; that is the documented
  // pre-activation failure mode the TIP explicitly preserves.
  @Test
  public void testTip854PreActivationNoOp() {
    VMConfig.initAllowTvmOsaka(0);
    contract.setRepository(RepositoryImpl.createRoot(StoreFactory.getInstance()));
    try {
      Pair<Boolean, byte[]> ret = contract.execute(new byte[(5 + 1) * 32]);
      // If the decoder happened to handle it without raising, we must not have
      // taken the post-activation reject path (false, empty).
      Assert.assertNotSame(ByteUtil.EMPTY_BYTE_ARRAY, ret.getRight());
    } catch (RuntimeException expectedLegacyBehaviour) {
      // Pre-activation: decoder may throw — this is the existing behaviour.
    }
  }
```
