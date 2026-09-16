### Title
Unchecked ABI offset/length in `ValidateMultiSign.execute()` allows out-of-bounds array read (`ArrayIndexOutOfBoundsException`) that is not caught by any try/catch, unlike its sibling precompile - ([File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java])

### Summary
`ValidateMultiSign` (TVM precompile address `0x…0a`) decodes its call-data into a `DataWord[]` and then walks it using attacker-controlled offsets/lengths taken directly from the call-data, with only a partial bounds check. `BatchValidateSign` (address `0x…09`) uses the exact same helper functions but wraps the whole decode+execute path in a `catch (Throwable)`. `ValidateMultiSign` does not — its top-level extraction logic executes completely unguarded, so a crafted `bytes[]` payload can throw an uncaught `ArrayIndexOutOfBoundsException` (or `OutOfMemoryError`/`NegativeArraySizeException` from an oversized `len`) straight out of the precompile.

### Finding Description
`ValidateMultiSign.execute()` reads an attacker-supplied "array length" word and an attacker-supplied "offset" word and immediately indexes into the parsed `DataWord[] words` array without validating that the resulting indices stay inside the array: [1](#0-0) 

The two helper functions it calls have inconsistent, incomplete guards:
- `extractBytes32Array` has **no bounds check at all** on `offset`, nor on `offset + i + 1` inside the loop: [2](#0-1) 
- `extractBytesArray` only checks the initial `offset`, but not `offset + i + 1` or the derived `bytesOffset`/`bytesLen` used deeper in the loop body: [3](#0-2) 
- `extractSigArray` checks only the initial `offset`, not the per-iteration `offset + i + 1`: [4](#0-3) 

`words[offset].intValueSafe()` (the "length") is fully attacker-controlled up to `Integer.MAX_VALUE`, so `for (int i = 0; i < len; i++)` will index `words[offset + i + 1]` far past the true array length the moment `offset` is chosen near the end of `words`. This mirrors the privoxy `chunked_body_is_complete()` bug class: a length/offset value taken from untrusted input is used to compute a buffer index without validating it stays within the buffer, causing an invalid read.

Crucially, `BatchValidateSign.execute()` wraps the identical helper calls (`extractBytes32Array`, `extractBytesArray`/`extractSigArray`) inside `doExecute()`, and the public `execute()` method catches `Throwable`: [5](#0-4) 

`ValidateMultiSign.execute()` has no such outer guard — the only `try/catch` in the method wraps the *later* permission-weighing loop, not the initial `DataWord.parseArray` / `extractBytesArray` / `extractSigArray` calls: [6](#0-5) 

### Impact Explanation
Any account can deploy or call a contract that issues a `CALL`/`STATICCALL` to the `validatemultisign` precompile with crafted `bytes[]` signature-array offsets, from an ordinary broadcast transaction (contract deployer/caller — no special privilege required, gated only by `VMConfig.allowTvmSolidity059()`, which is an activated chain feature, not a permission). If the resulting `ArrayIndexOutOfBoundsException`/`OutOfMemoryError` is not caught somewhere further up the TVM call stack (e.g., in `Program`'s precompile-invocation wrapper), it propagates as an unexpected `RuntimeException`/`Error` during transaction execution inside block application, which can crash or halt the executing node process — a denial-of-service matching the CVE's impact class (unexpected termination from an unvalidated length-driven out-of-bounds read).

### Likelihood Explanation
Reaching this path requires only a normal, unprivileged transaction that calls a contract invoking the `ValidateMultiSign` precompile with attacker-chosen ABI-encoded `bytes[]` data — well within reach of any contract deployer/caller once the TIP-Solidity059 feature is active on the network. The trigger condition (a length word chosen so that `offset + len + 1` exceeds `words.length`) is trivial to construct.

### Recommendation
Add the missing bounds checks to `extractBytes32Array`, `extractBytesArray`, and `extractSigArray` so that every index derived from attacker-controlled `intValueSafe()` values (`offset`, `offset + i + 1`, `bytesOffset`, `bytesLen`) is validated against `words.length`/`data.length` before use, returning an empty result (as `extractBytesArray` already does for its initial offset) instead of throwing. Additionally, wrap the entirety of `ValidateMultiSign.execute()` in the same defensive `try { ... } catch (Throwable t) { return Pair.of(true, DATA_FALSE); }` pattern already used by `BatchValidateSign.execute()`, so a decoding fault fails safely rather than propagating an uncaught exception.

### Proof of Concept
Not independently verified end-to-end (would require confirming whether the TVM's precompile-invocation site in `Program`/`VM` catches generic `RuntimeException`/`Error` from `PrecompiledContract.execute()`; this could not be confirmed with the available index). Conceptually:
1. Deploy/call a contract that performs a low-level `staticcall`/`call` to precompile address `0x000000000000000000000000000000000000000000000000000000000000000a` (`ValidateMultiSign`).
2. Craft the call-data so `words[3]` (the "signature array pointer" word) resolves to an `offset` near the end of the parsed `words` array, and `words[offset]` (the encoded array length) is set to a large value (e.g., `0xFFFFFFFF` truncated via `intValueSafe()`).
3. This causes `extractBytesArray`/`extractSigArray`'s loop to index `words[offset + i + 1]` beyond `words.length`, throwing `ArrayIndexOutOfBoundsException` with no enclosing catch in `ValidateMultiSign.execute()`.

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1036-1078)
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
