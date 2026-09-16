### Title
Uncontrolled memory allocation in TVM `ValidateMultiSign`/`BatchValidateSign` precompiles via unchecked array-length words - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The `extractBytes32Array` and `extractBytesArray` helper methods used by the `ValidateMultiSign` and `BatchValidateSign` TVM precompiled contracts read an attacker-controlled 32-byte word from the call data, convert it to an `int` via `DataWord.intValueSafe()`, and immediately use that value as the size of a newly allocated array, with no upper bound check on the length itself before the guarded fast-path is taken.

### Finding Description
`extractBytes32Array` and `extractBytesArray` take `len` directly from call data and allocate arrays sized by it: [1](#0-0) 

`len` comes from `words[offset].intValueSafe()`, and `DataWord.intValueSafe()` returns `Integer.MAX_VALUE` whenever the word occupies more than 4 bytes or decodes negative — i.e. it is fully attacker-controlled and can trivially be forced to `Integer.MAX_VALUE`: [2](#0-1) 

Both `ValidateMultiSign.execute` and `BatchValidateSign.doExecute` call these helpers with an offset word taken straight from the transaction/contract call data. A length cap (`MAX_SIZE = 16`) is only enforced **when `VMConfig.allowTvmSelfdestructRestriction()` is enabled** — i.e., a feature/hard-fork flag that must be voted in by the committee: [3](#0-2) [4](#0-3) 

If that proposal has not been activated on a given network (default-off feature gate), `extractBytesArray`/`extractBytes32Array` are invoked with no size check at all, so `len` can reach `Integer.MAX_VALUE`, causing `new byte[len][]` (an array of ~2^31 object references, i.e. multiple gigabytes on a 64-bit JVM) or `new byte[len][32]` to be allocated immediately. This mirrors the Exiv2 `PngChunk::parseChunkContent` bug class: an untrusted length field taken from input data is used to size a memory allocation without an upper bound, producing an uncontrolled allocation that can throw `OutOfMemoryError` (the Java analog of `std::bad_alloc`).

### Impact Explanation
An `OutOfMemoryError` thrown inside `Program.execute`/actuator code during a contract call is process-wide risk in the JVM — depending on the object size that fails to allocate and what else is executing concurrently, this can destabilize or crash the FullNode/SR process, which is a denial-of-service against a validator or full node servicing the transaction. Since `ValidateMultiSign`/`BatchValidateSign` are reachable by any deployed smart contract via a `CALL`/`STATICCALL` to their precompile addresses, this is reachable from a single, unprivileged, signed transaction or `triggerConstantContract`/`triggerSmartContract` API call.

### Likelihood Explanation
Exploitability depends on the `allowTvmSelfdestructRestriction` feature flag not being active on the target network. Where the proposal has already been activated, the `sigArraySize > MAX_SIZE` / `addrArraySize > MAX_SIZE` checks fully mitigate this before the unbounded allocation happens. On networks/branches where the flag is off (default state until committee activation), the guard is skipped entirely and the vulnerable path is directly reachable.

### Recommendation
Move the `MAX_SIZE` bound check (or an absolute sane cap, e.g., aligned with `WORD_SIZE`-derived call-data length) into `extractBytesArray` and `extractBytes32Array` themselves, unconditionally, rather than gating it behind `VMConfig.allowTvmSelfdestructRestriction()`. Additionally, validate that the derived `len` cannot exceed a value consistent with the actual `data.length` (similar to the `verifyLength` bounds check already used in `RLP.java`) before allocating any array.

### Proof of Concept
1. Deploy or use any contract that issues a `STATICCALL`/`CALL` to the `BatchValidateSign` precompile address on a network where `allowTvmSelfdestructRestriction` has not been activated.
2. Craft the call data so that `words[2]` (the address-array offset word) points to a word whose `intValueSafe()` resolves to a very large value (e.g. a word with 5+ significant bytes forces `Integer.MAX_VALUE`).
3. `extractBytes32Array(words, offset)` executes `new byte[Integer.MAX_VALUE][]`, which throws `OutOfMemoryError`/exhausts JVM heap during energy-metered execution, potentially destabilizing the node process handling the call.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L390-412)
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
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1066-1074)
```java
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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1165-1177)
```java
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

**File:** common/src/main/java/org/tron/common/runtime/vm/DataWord.java (L219-229)
```java
  /**
   * In case of int overflow returns Integer.MAX_VALUE otherwise works as #intValue()
   */
  public int intValueSafe() {
    int bytesOccupied = bytesOccupied();
    int intValue = intValue();
    if (bytesOccupied > 4 || intValue < 0) {
      return Integer.MAX_VALUE;
    }
    return intValue;
  }
```
