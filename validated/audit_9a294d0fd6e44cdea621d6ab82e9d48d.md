### Title
Unvalidated length field in `PrecompiledContracts.extractBytes32Array`/`extractBytesArray`/`extractSigArray` causes out-of-bounds array access from attacker-controlled calldata - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
`PrecompiledContracts` implements several TVM precompiled contracts (e.g. `BatchValidateSign`) that parse ABI-encoded calldata into a `DataWord[] words` array and then use helper methods to pull out dynamic arrays. These helpers read a "length" word directly from attacker-supplied calldata and use it as a loop bound to index further into the `words` array, without ever checking that the declared length actually fits within the bounds of `words`.

### Finding Description
`extractBytes32Array` reads the element count from calldata and loops over it without any bound check against `words.length`: [1](#0-0) 

`extractBytesArray` has the same pattern — an `offset > words.length - 1` guard exists only for the *header* index, but the derived `len` (also attacker-controlled) is never checked against the remaining array size before the loop indexes `words[offset + i + 1]`: [2](#0-1) 

`extractSigArray` repeats the identical unchecked pattern: [3](#0-2) 

This is the same bug class as the reported Vim CVE: a length/count value embedded inline in untrusted data is trusted and used to drive iteration over a buffer/array without validating that the buffer actually contains that many elements — the only "protection" (the initial `offset` bound check) guarantees room for a single header word, not for the full declared count, exactly mirroring the Vim flaw where only a floor check for one entry was performed.

There is a partial mitigating check in the same file, `isValidAbiEncoding`, which validates overall data length against expected header/item word counts: [4](#0-3) 
However, I was not able to confirm within the available search budget whether every call site of `extractBytes32Array`/`extractBytesArray`/`extractSigArray` (e.g., inside `BatchValidateSign.execute()`) invokes `isValidAbiEncoding` first, or whether an attacker can construct calldata that passes that check yet still supplies a per-array `len` field large enough to run `words[offset + i + 1]` past the end of `words`. This is a limitation of my analysis and would need to be verified directly in the `BatchValidateSign`/multi-sign precompile execute() implementation.

### Impact Explanation
If reachable with a `len` value larger than what the surrounding validation guarantees, `words[offset + i + 1]` will throw `ArrayIndexOutOfBoundsException` — a Java runtime exception, not a raw memory over-read as in the C-based Vim bug. Whether this becomes a node-crash/DoS or merely a reverted transaction depends entirely on whether the TVM's precompiled-contract invocation path wraps `PrecompiledContract.execute()` calls in a catch-all exception handler. I could not verify this wrapping within the iteration budget, so the ultimate severity (execution revert vs. an escaping unchecked exception that could destabilize block processing) is uncertain and should be confirmed by directly tracing the precompile dispatch code (e.g. `Program`'s call-to-precompile logic) and the `BatchValidateSign`/multi-sign `execute()` method.

### Likelihood Explanation
Precompiled contracts on TVM are reachable by any account via a plain `CALL`/`STATICCALL` from a deployed smart contract, so an unprivileged transaction sender can trigger this code path with attacker-chosen calldata at low cost (no special privileges required).

### Recommendation
Add an explicit bounds check before each array-length-driven loop in `extractBytes32Array`, `extractBytesArray`, and `extractSigArray`: verify `offset + len` (and, for `extractBytesArray`/`extractSigArray`, the derived `bytesOffset`/`bytesLen` indices) do not exceed `words.length`/`data.length` before performing any indexed access, returning a failure/empty result instead of throwing when the declared count exceeds the actual buffer contents — the same style of defensive check as `RLP.verifyLength` elsewhere in the codebase: [5](#0-4) 

### Proof of Concept
Construct calldata for the affected precompile (e.g., `BatchValidateSign`) where the ABI-encoded array-length word at the parsed `offset` is set to a large value (e.g. `0xFFFFFFFF` truncated via `intValueSafe()` to a large positive int) while the actual `words` array backing the calldata is short. When `extractBytes32Array`/`extractBytesArray` executes `words[offset + i + 1]` for `i` beyond the real array bounds, the JVM throws `ArrayIndexOutOfBoundsException`. Exact confirmation of end-to-end reachability (i.e., whether `isValidAbiEncoding` or other guards in `BatchValidateSign.execute()` block this specific value combination) requires reading the full `BatchValidateSign` execute() implementation, which was not fully retrieved in this session.

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L432-438)
```java
  private static boolean isValidAbiEncoding(byte[] data, int headerWords, int itemWords) {
    if (data == null || data.length % WORD_SIZE != 0) {
      return false;
    }
    long tail = subtractExact(data.length, multiplyExact(headerWords, WORD_SIZE));
    return tail > 0 && tail % multiplyExact(itemWords, WORD_SIZE) == 0;
  }
```

**File:** framework/src/main/java/org/tron/core/capsule/utils/RLP.java (L612-617)
```java
  private static void verifyLength(int suppliedLength, int availableLength) {
    if (suppliedLength > availableLength) {
      throw new RuntimeException(String.format("Length parsed from RLP (%s bytes) is greater "
          + "than possible size of data (%s bytes)", suppliedLength, availableLength));
    }
  }
```
