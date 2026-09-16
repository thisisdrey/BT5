### Title
Heap out-of-bounds read via unvalidated offsets in `PrecompiledContracts.extractBytes()`/`extractBytesArray()`/`extractSigArray()` - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
The `CTiffImg::WriteLine()` bug class is a heap-buffer-overflow caused by computing a write/read region from attacker-influenced header fields without validating those fields against the actual buffer size. The direct analog in java-tron is `PrecompiledContracts.extractBytes()`, which performs `Arrays.copyOfRange(data, offset, offset + len)` where `offset` and `len` are derived from attacker-controlled ABI-encoded calldata words, with no check that `offset + len <= data.length`.

### Finding Description
`extractBytes` blindly slices the precompiled-contract input buffer: [1](#0-0) 

It is invoked from two helper functions that build offsets/lengths purely from words parsed out of the same untrusted `data` buffer:

- `extractBytesArray` reads a length `len` and, for each entry, a `bytesOffset` and `bytesLen` directly from `words[...]` (attacker-supplied DataWords) and calls `extractBytes(data, (bytesOffset + offset + 2) * WORD_SIZE, bytesLen)` with no bound check that the computed slice fits within `data.length`: [2](#0-1) 

- `extractSigArray` follows the identical unchecked pattern for building a signature array, computing `bytesOffset` from `words[offset + i + 1]` and passing it straight to `extractBytes` with a fixed `SIG_LENGTH`, again without verifying the resulting range is within `data.length`: [3](#0-2) 

Only `offset > words.length - 1` is checked at the top of these helpers; the derived byte-level offsets computed from arbitrary `DataWord` values (`intValueSafe()`, which can be any 32-bit value) are never checked against `data.length` before being handed to `Arrays.copyOfRange`. This mirrors the ICC-profile bug: a size/offset field taken from untrusted input is used to compute a read/write window into a buffer without validating it stays within bounds, producing an out-of-bounds heap access.

### Impact Explanation
`Arrays.copyOfRange` with an out-of-range `to` index throws an unchecked `ArrayIndexOutOfBoundsException`/`NegativeArraySizeException` (for negative or excessive offsets/lengths). Because these helpers are reached from precompiled contracts invoked through ordinary TVM `CALL`/`STATICCALL` opcodes (e.g. batch-signature-validation precompiles), any transaction sender or contract can trigger the exception path with crafted calldata. Depending on how the calling precompiled-contract `execute()` method handles exceptions, this can propagate as an uncaught runtime exception during contract execution, causing abnormal termination of the current transaction execution and, if not uniformly caught across all node code paths that invoke this precompile, inconsistent node behavior between validating nodes for the same crafted transaction — a potential availability/consensus-consistency issue.

### Likelihood Explanation
The precompiled contracts exercising this code path are reachable by any unprivileged account through a standard smart-contract transaction; no special privileges are required, only the ability to craft calldata for the specific precompiled contract address that uses `extractBytesArray`/`extractSigArray`. The attacker fully controls the `DataWord` values used to compute `bytesOffset`/`bytesLen`, making the trigger deterministic and reliable.

### Recommendation
Add explicit bounds validation in `extractBytes`, `extractBytesArray`, and `extractSigArray` before calling `Arrays.copyOfRange`: verify `offset >= 0`, `len >= 0`, and `offset + len <= data.length` (using overflow-safe arithmetic), returning an empty array or rejecting the call (consistent with existing `isValidAbiEncoding` validation) when the computed window exceeds the buffer.

### Proof of Concept
Craft calldata to the batch-signature/multi-sign precompiled contract such that the length word at the array-length slot (`words[offset]`) or an individual offset word (`words[offset + i + 1]`) encodes a value that, once multiplied/added by `WORD_SIZE`/`SIG_LENGTH`/`bytesLen`, produces an `offset + len` far larger than the actual `data.length` (e.g., set the offset word to a large value like `0x7FFFFFFF / WORD_SIZE`). Submitting a transaction that calls this precompile with such calldata drives execution into `extractBytes`, where `Arrays.copyOfRange(data, offset, offset + len)` is invoked with `offset + len > data.length`, throwing an out-of-bounds exception during TVM execution of the precompiled contract call.

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
