### Title
Precompiled-contract ABI array decoders allocate memory from unvalidated attacker-controlled length words - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
The pypdf advisory (GHSA-f2v5-7jq9-h8cg) describes a RunLengthDecode stream whose length/count fields are trusted without being bounded against the actual available input, letting a small crafted file force a huge in-memory allocation. `PrecompiledContracts.extractBytesArray` and `PrecompiledContracts.extractSigArray` in java-tron exhibit the same bug class: they read an array-length word straight from TVM call data and immediately allocate a Java array of that size before any bound is enforced against the actual size of the call-data buffer.

### Finding Description
Both helper methods take a `len` value directly from `words[offset].intValueSafe()` — a 256-bit call-data word coerced to a safe `int` — and use it to allocate a top-level array with no check that `len` is proportional to the amount of data actually supplied: [1](#0-0) [2](#0-1) 

`intValueSafe()` only clamps the word into the `int` range; it performs no validation that the resulting `len` is consistent with `data.length`, the actual call-data buffer size. Because `data` is caller-supplied TVM call data (bounded only by the general transaction/data size limit, not by the semantic array-length field embedded inside it), an attacker can encode a call-data word claiming an extremely large `len` (up to `Integer.MAX_VALUE`) while sending only a small transaction. The line `byte[][] bytesArray = new byte[len][]` (or `byte[][] bytesArray = new byte[len][]` in `extractSigArray`) is evaluated before any per-element bound check, causing the JVM to attempt to allocate an array of `len` object references immediately — analogous to how `RunLengthDecode` blindly trusted an internal repeat/length byte to drive output-buffer growth without capping it to a sane bound derived from the real input size.

### Impact Explanation
A crafted smart-contract call that invokes the precompile using these decoders can force the executing full node to attempt a very large heap allocation (an array of millions of null references, or repeated across items when combined with the per-item `bytesLen` extracted similarly at line 407), leading to `OutOfMemoryError` / excessive GC pressure on the node processing the transaction. This matches the "node crash or halt" / API server denial-of-service impact class accepted for this scan, and does not require any privileged role — it is reachable by any account that can broadcast a `TriggerSmartContract` transaction that calls the affected precompiled contract, or by API callers issuing `triggerconstantcontract` queries through Wallet/HTTP that route into TVM execution.

### Likelihood Explanation
Likelihood is high for any address that can construct arbitrary call-data words: the only requirement is crafting a call-data blob whose header word for the array length is a large integer, independent of the number of bytes actually appended. No special privilege, deployed bytecode complexity, or state precondition is needed beyond being able to submit a transaction (or constant call) that reaches the code path invoking `extractBytesArray`/`extractSigArray`.

### Recommendation
Before allocating `new byte[len][]` (and similarly the inner `extractBytes` allocation sized by `bytesLen`), validate `len` (and each `bytesLen`) against the maximum number of words actually available in `data` (i.e., `(data.length / WORD_SIZE)` and the remaining payload after the current offset), rejecting/aborting with an out-of-bounds/invalid encoding error rather than attempting the allocation. Consider capping `len`/`bytesLen` to a small sane constant tied to `data.length` and to the energy already charged for the call, mirroring the bound already present in `isValidAbiEncoding` and `verifyLength` (`framework/src/main/java/org/tron/core/capsule/utils/RLP.java`) used elsewhere in the codebase for RLP length parsing.

### Proof of Concept
1. Craft a `TriggerSmartContract` transaction whose call data targets the precompile that invokes `extractBytesArray` (or `extractSigArray`), placing at the `offset` word a value like `0x7FFFFFFF` for `len`.
2. Append only a minimal amount of trailing data (well under the size implied by `len`).
3. Broadcast/execute the transaction (or issue it as a constant call via the JSON-RPC/HTTP `triggerconstantcontract` endpoint).
4. `extractBytesArray`/`extractSigArray` executes `new byte[len][]` before validating that `len` corresponds to the actual call-data size, driving the node to attempt to allocate an array of ~2^31 references, exhausting available heap and potentially crashing or hanging the node's transaction-processing/API-serving thread.

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
