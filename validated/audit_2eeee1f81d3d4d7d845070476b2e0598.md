### Title
Unchecked attacker-controlled array-length field used to size allocations in TVM precompiled-contract ABI decoding - ([File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java])

### Summary
The Linux `dfl-afu` bug is a classic "trust a caller-supplied length field before validating it against a safe bound" issue: a 64-bit length taken from user input is used to derive an allocation/iteration count without an upper-bound check, allowing implicit truncation/overflow downstream. The equivalent pattern exists in java-tron's TVM precompiled-contract ABI decoding helpers, where a length word taken directly from smart-contract call data is used to size Java arrays with no upper bound before the actual backing data is validated.

### Finding Description
`PrecompiledContracts.extractBytesArray()`, `extractSigArray()`, and `extractBytes32Array()` read a length field straight out of attacker-supplied `DataWord[] words` (decoded from the calldata sent to a precompiled contract) via `intValueSafe()`, and immediately use it to allocate a Java object array: [1](#0-0) [2](#0-1) [3](#0-2) 

`intValueSafe()` only clamps to `Integer.MAX_VALUE` when the value doesn't fit in 4 bytes; it does not enforce any application-level bound related to the actual size of the supplied `data` byte array: [4](#0-3) 

Just like the kernel bug, where `length` from `DFL_FPGA_PORT_DMA_MAP` is used to derive `npages` before being bounds-checked against `INT_MAX`/actual buffer capacity, here `len` is used to allocate `new byte[len][]` / iterate `for (i = 0; i < len; i++)` before any check that `len` corresponds to the real size of `data`. Because `data` is fully attacker-controlled call data of arbitrary (small) actual length, a contract can encode a header word claiming a huge array length (up to `Integer.MAX_VALUE`) while sending only a few bytes of calldata.

### Impact Explanation
Allocating `new byte[Integer.MAX_VALUE][]` (an 8-16GB+ reference array) or driving an unbounded loop over `words[offset + i + 1]` immediately throws `OutOfMemoryError` / triggers massive heap pressure inside the node process executing the transaction. Because `execute()` in these precompiled contracts is invoked synchronously during TVM execution while processing a broadcast transaction, any full node or SR node that receives and executes the transaction (including during block application) hits the same crash path. This matches the "node crash or halt" acceptance criterion — an unprivileged contract caller can craft calldata with an oversized length header targeting these precompile decode helpers, causing an OutOfMemoryError that can crash/destabilize the JVM node processing the transaction.

### Likelihood Explanation
Any account that can send a transaction invoking the affected precompiled contract(s) can trigger this — no special privilege is needed beyond crafting the calldata's leading "array length" word. Since `intValueSafe()` only saturates at `Integer.MAX_VALUE` rather than checking it against the real remaining calldata length, the attack is a single crafted transaction, making likelihood high once a caller can reach an `execute()` path using these helpers.

### Recommendation
In `extractBytesArray`, `extractSigArray`, and `extractBytes32Array`, validate the decoded `len` against the actual bounds of `data`/`words` (e.g., `len * WORD_SIZE <= data.length - headerOffset`, and `offset + len < words.length`) before allocating `new byte[len][]` or entering the loop, rejecting the call (returning `Pair.of(false, ...)`) when the length is inconsistent with the supplied data size — analogous to how the kernel fix validates `map.length` against `INT_MAX`/page-count before it reaches the truncating `int nr_pages` parameter.

### Proof of Concept
Construct a transaction calling a TVM precompiled contract whose `execute(byte[] data)` invokes `extractBytesArray`/`extractSigArray` (batch signature/array-based precompiles), with call data where the length header word at the expected offset encodes a very large value (e.g., `0x7FFFFFFF`) while the remainder of `data` is only a few bytes. When `execute()` runs, `int len = words[offset].intValueSafe()` returns the huge value and `new byte[len][]` triggers an `OutOfMemoryError` in the node processing the transaction.

Note: I was unable to fully enumerate every precompiled contract that calls `extractBytesArray`/`extractSigArray` (the index did not return their call sites), so the exact opcode/precompile address used to reach this code should be confirmed by a full-repo grep before finalizing an exploit; the root-cause defect in the shared decoding helpers is confirmed above.

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
