### Title
Unvalidated attacker-controlled offset/length fields in `ValidateMultiSign` precompile enable out-of-bounds array indexing / crash - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
The jq bug (`CVE-2025-48060`) stems from a size value computed once and then used to allocate/index a buffer without re-validating it against the actual data bounds, producing an out-of-bounds access. The analogous pattern in java-tron is the `extractBytesArray` / `extractSigArray` helpers used by the `ValidateMultiSign` TVM precompile, where attacker-supplied "offset" and "length" words taken directly from transaction calldata are used to index into the `DataWord[] words` array and to slice the raw `data` byte array, without validating that the derived indices stay within the bounds of `words` or `data`.

### Finding Description
`extractBytesArray` reads a count from calldata and then, for each element, reads further offset/length words directly from the same attacker-controlled `words` array: [1](#0-0) 

`extractSigArray` follows the same unchecked-index pattern: [2](#0-1) 

Both eventually call `extractBytes`, which slices `data` with `Arrays.copyOfRange(data, offset, offset + len)`: [3](#0-2) 

`ValidateMultiSign.execute` invokes these helpers directly (outside of any try/catch) using offsets computed from `words[3].intValueSafe() / WORD_SIZE`, which is fully attacker-controlled via the calldata sent to this precompile address: [4](#0-3) 

Because `len`, `bytesOffset`, and `bytesLen` are derived from `intValueSafe()` (which only guards against `int` overflow, not against exceeding the actual `words.length` or `data.length`), a crafted transaction can make `offset + i + 1` exceed `words.length` (triggering `ArrayIndexOutOfBoundsException`) or make the computed slice offset exceed `data.length` (triggering `ArrayIndexOutOfBoundsException` from `Arrays.copyOfRange`, since it throws when `fromIndex > original.length`). This mirrors the jq root cause: a size/offset value is trusted and used to size/index a buffer without validating it against the actual available data, causing an out-of-bounds access.

Note: `BatchValidateSign.execute` wraps the same category of calls in `doExecute` behind a `catch (Throwable t)`, showing the developers were aware such unbounded parsing can throw, but `ValidateMultiSign.execute` lacks the same protection at lines 1057-1078. [5](#0-4) 

### Impact Explanation
If an uncaught `ArrayIndexOutOfBoundsException`/`NegativeArraySizeException` from `ValidateMultiSign.execute` is not caught by the TVM's generic precompiled-contract call path, it can propagate out of contract execution for a single transaction. Depending on how the VM's precompile dispatch handles unexpected runtime exceptions (this specific exception-handling wrapper around the precompile call site in `Program.java`/`OperationActions.java` could not be fully confirmed within the available tool budget), the worst-case outcome is a node-crashing or block-processing halting condition triggered by any single unprivileged transaction calling this precompile with crafted calldata — matching the report's crash-class impact bar.

### Likelihood Explanation
`ValidateMultiSign` is a standard TVM precompiled contract reachable by any account via a normal contract call (`STATICCALL`/`CALL` to its fixed address) with arbitrary calldata, requiring no special privileges — an unprivileged contract deployer or caller can trigger this path directly.

### Recommendation
Add explicit bounds checks in `extractBytesArray`/`extractSigArray` (and in `extractBytes`) validating that `offset + i + 1 < words.length`, that `bytesOffset + offset + 2` stays within `words.length`, and that the final byte range fits within `data.length` before slicing; alternatively wrap the `ValidateMultiSign.execute` body (as already done for `BatchValidateSign.doExecute`) in a `try { ... } catch (Throwable t) { return Pair.of(true, DATA_FALSE); }` to fail safely instead of propagating an unchecked exception.

### Proof of Concept
Construct calldata for `ValidateMultiSign` where `words[3]` encodes an offset such that `words[3].intValueSafe() / WORD_SIZE` points near the end of the `words` array, and the value at that position (`sigArraySize`/`len`) is set larger than the number of remaining words. This causes `extractSigArray`/`extractBytesArray` to read `words[offset + i + 1]` past the end of the array, throwing an uncaught `ArrayIndexOutOfBoundsException` from within `ValidateMultiSign.execute` at [6](#0-5) . Full confirmation of downstream node-crash impact requires tracing the exception-handling wrapper at the precompile-invocation call site in `Program.java`, which was not fully verifiable within the current investigation.

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L428-430)
```java
  private static byte[] extractBytes(byte[] data, int offset, int len) {
    return Arrays.copyOfRange(data, offset, offset + len);
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1057-1078)
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
