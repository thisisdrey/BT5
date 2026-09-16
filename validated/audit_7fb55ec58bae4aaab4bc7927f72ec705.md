### Title
Unbounded array allocation from attacker-controlled length in precompiled-contract signature-batch decoding causes OOM/DoS - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The HDF5 CVE (`H5F_addr_decode_len`) is a heap corruption/DoS caused by trusting an unvalidated length field read from untrusted input to size a buffer. The same bug class exists in java-tron's TVM precompiled-contract helpers `extractBytes32Array` and `extractBytesArray`, which read a length directly from attacker-supplied call data and use it to allocate arrays with no upper bound or consistency check against the actual calldata size.

### Finding Description
`extractBytes32Array` and `extractBytesArray` derive `len` purely from a `DataWord` supplied in the precompile's input: [1](#0-0) 

`len = words[offset].intValueSafe()` is attacker-controlled and is used immediately to allocate `new byte[len][]` before any check that `len` corresponds to the real number of remaining words in `data`. `extractBytesArray` only checks `offset > words.length - 1`, not that `len` itself is bounded by the remaining array length, and `extractBytes32Array` has no bounds check at all. A crafted contract call (or a transaction invoking a contract that calls this precompile) can set this length field to a very large value (e.g. close to `Integer.MAX_VALUE`), forcing the VM thread to attempt a huge array allocation. This is directly analogous to the HDF5 bug class: an untrusted length field decoded from input data is used to size a buffer without validating it against the bounds of the actual data being parsed.

`extractSigArray` has the same pattern for signature extraction: [2](#0-1) 

### Impact Explanation
An oversized `len` triggers a large heap allocation attempt (`new byte[len][]`), which can throw `OutOfMemoryError` or, for negative decoded values, `NegativeArraySizeException`. Because this happens inside precompiled-contract execution during TVM opcode dispatch, an uncaught `OutOfMemoryError`/runtime error at this layer can crash or destabilize the executing node (denial of service), matching the CVE's "could cause a Denial of Service" impact, and is reachable from any unprivileged smart-contract call that invokes the affected precompile.

### Likelihood Explanation
Reachable from a single, unprivileged transaction: any account can deploy or call a contract that invokes the precompile with crafted calldata containing an inflated length word. Because the length is read straight from `DataWord.intValueSafe()`, no special privilege beyond deploying/calling a contract is required, making this trivially reachable via the standard contract-call actuator path.

### Recommendation
- Bound `len` in `extractBytes32Array`, `extractBytesArray`, and `extractSigArray` to a sane maximum and to the number of words actually available in `words`/`data` before allocating arrays.
- Validate `bytesOffset`, `bytesLen`, and derived offsets against `data.length` before use, mirroring the `verifyLength`-style checks already used in `framework/src/main/java/org/tron/core/capsule/utils/RLP.java` (`calcLength`/`verifyLength`) for RLP length fields.
- Charge/limit energy proportional to the requested length before allocation so an attacker cannot get a large allocation for negligible cost.

### Proof of Concept
Construct a transaction calling a contract that invokes the affected precompile (multi-signature/batch verification precompile using `extractBytesArray`/`extractBytes32Array`) with input data where the length word at the expected `offset` is set to a very large value (e.g. `0x7FFFFFFF`). On execution, `extractBytes32Array`/`extractBytesArray` will attempt `new byte[0x7FFFFFFF][]`, causing an `OutOfMemoryError` in the node's VM execution thread.

Note: I could not fully confirm from the indexed code whether an outer try/catch in the VM execution path (e.g., in `VMActuator`/`Runtime`) currently catches `OutOfMemoryError`/`Throwable` around precompile execution, which would affect the exact severity (isolated transaction failure vs. broader node instability). This should be verified in a full checkout, and I recommend a Devin session with full repository access to confirm the precompile's exact call site, current exception-handling wrapper (if any), and to implement the bounds-check fix.

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
