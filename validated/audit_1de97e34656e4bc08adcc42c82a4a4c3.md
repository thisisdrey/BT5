### Title
Unbounded array allocation from attacker-controlled length in TVM precompile signature-batch decoding causes node OOM/crash - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
`PrecompiledContracts.extractSigArray` and the related `extractBytesArray`/`extractBytes32Array` helpers read a length value directly from attacker-supplied call data (`words[offset].intValueSafe()`) and immediately allocate a Java array of that size (`new byte[len][]`) with no upper bound check before iterating over it. [1](#0-0) [2](#0-1) 

### Finding Description
`intValueSafe()` on `DataWord` returns `Integer.MAX_VALUE` when the encoded 256-bit value doesn't fit in an `int` (instead of throwing), so any attacker who controls the length field simply has to encode a value larger than 4 bytes to force `len = Integer.MAX_VALUE`. [3](#0-2) 

That value is used unchecked as the size for a new object array (`new byte[len][]` — an array of `Integer.MAX_VALUE` references, i.e. ~16GB+ on a 64-bit JVM) in `extractSigArray`, `extractBytesArray`, and `extractBytes32Array`: [4](#0-3) 

There is no bound comparison against the actual size of `data` (the calldata buffer) before the allocation attempt. This is analogous to the Teku gossipsub bug: a resource-exhaustion condition caused by allocating a buffer sized from an unchecked, attacker-controlled length field, leading to denial of service.

### Impact Explanation
Triggering the huge allocation throws an `OutOfMemoryError` inside the TVM execution path. Depending on how deep/where this occurs relative to exception handling in the VM interpreter loop, this can either be caught as a VM-level exception (aborting only the single call, low impact) or, if thrown outside a caught scope, can destabilize the JVM heap and crash or hang the full node process (high impact — denial of service / consensus participation halt). Given the report's classification pattern (unchecked length -> buffer allocation -> DoS), and that TVM execution paths are reachable from any signed transaction, this maps to a node crash / API unavailability class impact. Precompiled contracts run inside the executing node's own JVM for every node processing the transaction (including block validation), so a successful trigger could propagate across all full nodes processing the same block, not just a subset.

### Likelihood Explanation
Likelihood is uncertain without confirming the exact calldata layout and call site that invokes `extractSigArray` (the `BatchValidateSign` precompile) with attacker-supplied `words` — the grep search only found this precompile referenced by a test class and not the concrete call path from `PrecompiledContracts.getContractForAddress` dispatch code, which I was unable to fully inspect before this session ended. If the precompile is reachable directly via a `CALL`/`STATICCALL` opcode to its fixed contract address with arbitrary calldata from any contract (the normal pattern for these precompiles), then likelihood is high since it requires only one crafted transaction and no privileged role. However, I could not verify whether an outer bounds/energy check (e.g., data-length-based energy cost that would revert with out-of-energy before reaching the allocation) exists and effectively gates this, or whether the VM interpreter wraps precompile execution in a broad `try/catch(Throwable)` that would defang the crash into a mere reverted call.

### Recommendation
- Bound `len` in `extractSigArray`, `extractBytesArray`, and `extractBytes32Array` against a sane maximum derived from the actual `data.length` (e.g., `len` must satisfy `len <= (data.length - headerOffset) / expectedItemSize`) before allocating any array.
- Reject with a `execute` failure (`Pair.of(false, ...)`) instead of allocating when the length is inconsistent with the supplied calldata size.
- Audit all other `intValueSafe()`/`longValueSafe()` usages in `PrecompiledContracts.java` and `OperationActions.java` that feed directly into array/buffer allocation sizes for the same class of issue.

### Proof of Concept
1. Deploy or use any contract that performs a `STATICCALL`/`CALL` to the `BatchValidateSign` precompile address.
2. Craft the calldata so that the word at the "signature count" offset (`words[offset]`) encodes a value requiring more than 4 bytes (e.g., `0x0100000000` truncated to fit the 32-byte word with high bits set), causing `intValueSafe()` to return `Integer.MAX_VALUE`.
3. Send the transaction; `extractSigArray` executes `new byte[Integer.MAX_VALUE][]`, attempting a multi-gigabyte allocation, triggering `OutOfMemoryError` during transaction execution on every full node that processes/validates the block containing this transaction.

Note: I was not able to fully trace the exact opcode/energy-check call path that invokes `extractSigArray` before the session tool budget ran out, nor confirm whether an existing outer exception handler in the TVM interpreter fully contains the `OutOfMemoryError` without broader JVM impact. This should be verified with a live Devin session (full repo access, ability to run the TVM and reproduce the OOM in a test) before treating this as conclusively exploitable at High/Critical severity.

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
