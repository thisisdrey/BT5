### Title
Unbounded attacker-controlled array length in `extractBytesArray`/`extractSigArray` causes out-of-bounds read in BatchValidateSign precompile - ([File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java])

### Summary
The libsoup CVE-2025-4969 root cause is a boundary-parsing routine that trusts an attacker-supplied length field to walk past the end of an allocated buffer because the terminator/bounds check is off by one. The analogous pattern exists in java-tron's TVM precompile input parsers `extractBytesArray` and `extractSigArray`, which read a length word directly from attacker-controlled call data and then index into a fixed-size `DataWord[] words` array using that length without validating it against `words.length`.

### Finding Description
`extractBytesArray` and `extractSigArray` in `PrecompiledContracts.java` only check that `offset` itself is within bounds of `words`, but then use an unvalidated, attacker-controlled `len` (read directly from the call-data word at `offset`) to loop and index `words[offset + i + 1]`: [1](#0-0) 

Specifically:
- `if (offset > words.length - 1) return new byte[0][];` only bounds-checks `offset`, not `offset + len`.
- `int len = words[offset].intValueSafe();` is fully attacker controlled (up to `Integer.MAX_VALUE`, since `intValueSafe()` clamps but doesn't reject large values).
- The subsequent loop `for (int i = 0; i < len; i++) { ... words[offset + i + 1] ... }` will throw `ArrayIndexOutOfBoundsException` once `offset + i + 1 >= words.length`, or in `extractBytesArray`, silently compute a bogus `bytesOffset`/`bytesLen` from adjacent (out-of-range) memory before the array-index exception is thrown — this mirrors the "failure to correctly verify the termination"/off-by-one class in the report, since the length-derived loop bound is never checked against the actual buffer length before use.

This mirrors `extractBytes32Array` at lines 390-397, which has the identical unguarded pattern (`words[offset]` length with no upper bound check at all). [2](#0-1) 

These helpers are used by the `BatchValidateSign` and `ValidateMultiSign` precompiled contracts (addresses `0x9`/`0xa`), which are reachable from any deployed smart-contract call once `VMConfig.allowTvmSolidity059()` is enabled: [3](#0-2) 

### Impact Explanation
An attacker who deploys or calls a contract that invokes the `BatchValidateSign`/`ValidateMultiSign` precompile can craft `data` such that the decoded `DataWord[] words` array is short while the length field read from it is large. This drives the loop to index past the end of `words`, throwing an unhandled `ArrayIndexOutOfBoundsException` deep in native precompile execution logic. Depending on how the surrounding TVM execution/exception handling treats this (it is not one of the caught `Exception`/`Program.Exception` types normally expected from a well-formed VM op), this can crash the executing node thread or, if uncaught during block application, cause inconsistent execution results between nodes that validate differently — a potential denial-of-service or consensus-divergence risk for a component reachable from ordinary contract calls.

### Likelihood Explanation
High reachability: any account can deploy a contract that calls the precompiled address for `BatchValidateSign`/`ValidateMultiSign` with attacker-controlled `data`, requiring no special privileges — only that the chain has `allowTvmSolidity059` enabled (a mainnet-active feature). Triggering the bug only requires supplying a call-data length word (`words[offset]`) larger than the number of remaining words actually present in the payload.

### Recommendation
Add an explicit bounds check on the derived length before iterating: verify `offset + len + 1 <= words.length` (and, for `extractBytesArray`, that `bytesOffset` and the corresponding length word index also stay within `words.length`) before use, returning an empty array or throwing a normal `Program.Exception`/VM-level revert instead of allowing raw `ArrayIndexOutOfBoundsException` to propagate. Apply the same fix to `extractBytes32Array`.

### Proof of Concept
Craft a call to the `BatchValidateSign` precompile with `data` encoded so that the header word count (`words.length`) is small (e.g., 3 words) but the length word at the expected array offset decodes to a large value (e.g., `0xFFFFFFFF` truncated via `intValueSafe()` to a large positive int). This causes `extractBytesArray`/`extractSigArray` to attempt `words[offset + i + 1]` for `i` values that exceed `words.length`, throwing `ArrayIndexOutOfBoundsException` inside precompile execution rather than a clean, caught VM-level error.

*Note: I was unable to fully trace the exact exception-handling wrapper around precompile `execute()` calls in the TVM interpreter dispatch loop within the available index, so I cannot confirm with full certainty whether this exception is caught gracefully at a higher layer (in which case impact would be limited to a reverted transaction rather than a node crash). A Devin session with full repo access would be needed to trace `Program.callToPrecompiledAddress` or equivalent dispatch code and confirm the exact blast radius.*

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L254-259)
```java
    if (VMConfig.allowTvmSolidity059() && address.equals(batchValidateSignAddr)) {
      return batchValidateSign;
    }
    if (VMConfig.allowTvmSolidity059() && address.equals(validateMultiSignAddr)) {
      return validateMultiSign;
    }
```

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
