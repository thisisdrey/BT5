## Title
Attacker-controlled array length in `PrecompiledContracts.extractBytes32Array` causes uncontrolled array allocation / out-of-bounds indexing during TVM precompile execution - ([File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java])

### Summary
`PrecompiledContracts.extractBytes32Array(DataWord[] words, int offset)` reads an array-length field directly from attacker-controlled TVM call data and uses it, without any upper- or lower-bound validation, both to size a new Java array and to index into the `words` array in a loop:

```java
private static byte[][] extractBytes32Array(DataWord[] words, int offset) {
  int len = words[offset].intValueSafe();
  byte[][] bytes32Array = new byte[len][];
  for (int i = 0; i < len; i++) {
    bytes32Array[i] = words[offset + i + 1].getData();
  }
  return bytes32Array;
}
``` [1](#0-0) 

This is directly analogous to the Bento4 `AP4_CttsTableEntry` bug class: a size/count value taken straight from untrusted input is used to drive array allocation and a subsequent write/read loop with no bound check against the actual size of the backing buffer.

### Finding Description
`len` is derived from `words[offset].intValueSafe()`, which only guarantees the value fits into a Java `int` — it does not validate that `len` is non-negative or that `offset + len` stays within the bounds of the `words` array that was parsed from the raw precompile call data. Two sibling helpers in the same file follow the identical unsafe pattern, `extractBytesArray` and `extractSigArray`, which read `len` from call data and then loop `i` from `0` to `len`, indexing `words[offset + i + 1]` and `words[offset + bytesOffset + 1]` without checking `offset + i + 1 < words.length`: [2](#0-1) 

Because `words` is built by splitting the precompile's `data` argument into fixed 32-byte `DataWord` chunks, its length is entirely determined by the size of the calldata supplied by the caller. A caller can therefore supply calldata whose declared "array length" word is far larger than the number of actual `DataWord` chunks present, causing the loop to index past the end of `words` — or supply a negative length that triggers `NegativeArraySizeException` from `new byte[len][]`. Unlike the `AP4_CttsTableEntry` C++ case, the Java runtime does not corrupt memory on out-of-range array access; instead it throws an unchecked exception (`ArrayIndexOutOfBoundsException` or `NegativeArraySizeException`) that is not one of the checked/caught exception types the precompile execution path expects.

### Impact Explanation
An unhandled runtime exception thrown from inside precompile execution during TVM opcode dispatch can propagate up through the actuator/TVM execution path instead of being converted into a normal VM revert. If it is not caught by the standard `Program.Exception` handling used for reverts, it can abort transaction execution in a way inconsistent with normal TVM semantics, causing a node to fail to process an otherwise valid block/transaction (denial of service for the executing node) or produce inconsistent execution results between nodes that handle the uncaught exception differently, risking a chain split.

### Likelihood Explanation
Reachability depends on which precompile(s) actually call `extractBytes32Array`/`extractBytesArray`/`extractSigArray` and whether that precompile is reachable via a plain `CALL` from an unprivileged smart contract (e.g. as part of `BatchValidateSign` or a similar precompiled contract). I was not able to fully confirm, within available tool budget, which specific precompiled contract's `execute()` method invokes these helper functions, nor whether an outer bounds check on `data.length` already prevents the crafted-length scenario before these helpers are reached. This uncertainty affects confidence that the path is reachable from an unprivileged, unauthenticated transaction without any additional privilege.

### Recommendation
Add explicit bounds validation before allocation/indexing in `extractBytes32Array`, `extractBytesArray`, and `extractSigArray`: reject (return empty array or treat call as failed) when `len < 0` or `offset + len + 1 > words.length`, mirroring the defensive checks already present in `ContractEventParser.subBytes` (`start < 0 || start >= src.length || length < 0 || length > src.length - start`) at [3](#0-2) .

### Proof of Concept
Conceptual PoC (requires confirming the calling precompile to fully weaponize):
1. Craft a smart-contract call to the precompile that internally invokes `extractBytes32Array`/`extractBytesArray`, supplying calldata whose length-word at the expected `offset` is set to a very large positive value (e.g. `0x7fffffff`) or to `0xFFFF...FFFF` (interpreted as negative by `intValueSafe`), while the actual calldata is only a few `DataWord`s long.
2. On `new byte[len][]` with a huge `len`, either an `OutOfMemoryError`/`NegativeArraySizeException` is thrown; with a moderate `len` that exceeds `words.length`, the subsequent loop throws `ArrayIndexOutOfBoundsException` when accessing `words[offset + i + 1]`.
3. Observe whether the exception is caught and converted to a normal revert, or propagates and disrupts transaction/block execution — this final step needs runtime verification, which was not possible in this analysis session.

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L399-426)
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

**File:** framework/src/main/java/org/tron/common/logsfilter/ContractEventParser.java (L80-90)
```java
  protected static byte[] subBytes(byte[] src, int start, int length) {
    if (ArrayUtils.isEmpty(src)) {
      throw new OutputLengthException("source data is empty");
    }
    if (start < 0 || start >= src.length || length < 0 || length > src.length - start) {
      throw new OutputLengthException(
          "data start:" + start + ", length:" + length + ", src.length:" + src.length);
    }
    byte[] dst = new byte[length];
    System.arraycopy(src, start, dst, 0, length);
    return dst;
```
