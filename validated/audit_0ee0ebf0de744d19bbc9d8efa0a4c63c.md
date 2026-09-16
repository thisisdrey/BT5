### Title
Precompiled-contract calldata length fields drive unbounded array allocation without validation - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
`PrecompiledContracts.extractBytes32Array`, `extractBytesArray`, and `extractSigArray` read a length value directly out of attacker-controlled EVM calldata (`words[offset].intValueSafe()`) and immediately use it to size a Java array (`new byte[len][]`), with no upper bound check against the actual size of the supplied `data`/`words` buffer. [1](#0-0) 

### Finding Description
`intValueSafe()` in `DataWord` clamps any value whose big-endian representation occupies more than 4 bytes (or that would be interpreted as negative) to `Integer.MAX_VALUE`, but it does not reject values that are simply "too large for the actual payload." [2](#0-1) 

This value (`len`, up to `Integer.MAX_VALUE`) is then used as the dimension of a Java array-of-arrays allocation, `new byte[len][]` in `extractBytes32Array` / `extractBytesArray`, and `new byte[len][]` in `extractSigArray`, before any element is copied from the real calldata: [3](#0-2) 

This mirrors the root cause of CVE-2026-31970: an untrusted length/count field taken from external input is used to size a heap allocation before the code has verified that the input actually contains that many records/bytes. In the HTSlib case the miscalculated size under-allocates and then a fixed write plus later frees corrupt the heap; here the equivalent effect is allocating a wildly oversized array (up to 2^31-1 references) purely from a hostile length word, without first checking it against `words.length` or `data.length`.

### Impact Explanation
Any contract call that reaches one of these helper methods with a crafted length word can force the TVM executor thread to attempt an allocation of an enormous `byte[Integer.MAX_VALUE][]` object array. This throws `OutOfMemoryError`, which is an unchecked `Error`, not an `Exception` — depending on how far up the call stack it propagates before being caught it can abort the executing thread and, in a JVM already near its heap ceiling, force the JVM to fail other unrelated threads or halt the node process, i.e., a node crash / potential chain halt reachable from a single, unprivileged, signed transaction that calls the affected precompile.

### Likelihood Explanation
The precompiled contracts in this file are invoked whenever any smart contract calls the fixed precompile address (e.g. via `CALL`/`STATICCALL` opcodes), which any account can trigger by deploying or invoking a trivial contract. Constructing calldata whose ABI "length" word is a large value (e.g., near `0xFFFFFFFF`) is trivial for any transaction broadcaster; no special privilege, node cooperation, or witness/committee role is required.

### Recommendation
Validate `len` against the number of remaining `words`/`data` bytes before allocating (e.g., reject if `len > (words.length - offset - 1)` or an equivalent bound derived from `data.length`), mirroring the fix pattern applied elsewhere in the same file such as `isValidAbiEncoding` and `verifyLength` in `RLP.java`, which explicitly compare a supplied length against the available buffer size before trusting it for allocation. [4](#0-3) [5](#0-4) 

### Proof of Concept
1. Deploy (or reuse) a contract that performs a low-level `call`/`staticcall` to the precompiled-contract address whose executor invokes `extractBytes32Array`/`extractBytesArray`/`extractSigArray` (the batch-signature-validation style precompile in this file).
2. Craft the calldata so the DataWord at the expected "array length" offset encodes a very large 32-bit value (e.g., `0x7FFFFFFF`), while the remainder of the calldata is short/empty.
3. Broadcast the transaction; `intValueSafe()` returns the large value unchanged (it is only clamped for >4-byte-wide values), and `new byte[len][]` is executed before any bounds check against the real calldata size, causing the executing node thread to throw `OutOfMemoryError` while attempting the allocation.

Note: I could not fully confirm from the indexed content which specific top-level precompile (`BatchValidateSign` or another) calls these three helper methods, since the call sites were outside the retrieved snippet ranges; a Devin session with full file access would be needed to pinpoint the exact public `execute()` entry point and confirm gas/energy metering does not already bound `len` before these calls.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L390-426)
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

**File:** framework/src/main/java/org/tron/core/capsule/utils/RLP.java (L612-617)
```java
  private static void verifyLength(int suppliedLength, int availableLength) {
    if (suppliedLength > availableLength) {
      throw new RuntimeException(String.format("Length parsed from RLP (%s bytes) is greater "
          + "than possible size of data (%s bytes)", suppliedLength, availableLength));
    }
  }
```
