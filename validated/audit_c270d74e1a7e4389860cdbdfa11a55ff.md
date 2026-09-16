Based on my investigation, the closest analog to the ImageMagick bug class (unchecked, attacker-influenced size used to allocate a buffer, causing an out-of-bounds/uncontrolled-size write) is in `PrecompiledContracts.java`'s ABI-decoding helpers used by the `BatchValidateSign` and `ValidateMultiSign` precompiles.

### Title
Uncontrolled array-size allocation from unvalidated ABI length field in TVM precompile signature parsers - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
`extractSigArray`, `extractBytesArray`, and `extractBytes32Array` in `PrecompiledContracts.java` read a length value directly out of attacker-supplied ABI-encoded `data` (via `words[offset].intValueSafe()`) and immediately use it to allocate a Java array (`new byte[len][]`) and to index further into the `words` array, with no upper bound check against the actual `data`/`words` size before allocation. [1](#0-0) [2](#0-1) 

### Finding Description
These helpers are called from the `BatchValidateSign` precompile execution path, which is reachable by any TVM contract call (an unprivileged transaction can trigger it via `CALL`/staticcall to the precompile address). The `len` field is taken from attacker-controlled call `data` without validating it against the bounds of the `words` array or against `data.length`, unlike `extractBytesArray`'s sibling checks which only bound-check `offset`, not the derived `len`. Because `len` is attacker-controlled and unbounded (`intValueSafe()` can return up to `Integer.MAX_VALUE`), the subsequent `new byte[len][]` allocation and the loop indexing `words[offset + i + 1]` can:
- throw `NegativeArraySizeException` or `OutOfMemoryError` when `len` is crafted to be negative or extremely large, or
- throw `ArrayIndexOutOfBoundsException` when `offset + i + 1` exceeds `words.length`.

This mirrors the ImageMagick sixel-encoder bug class: an unchecked, attacker-influenced size value drives a buffer allocation/copy without validating it fits the available buffer, and the resulting failure path (uncaught allocation/array exception) is not otherwise gracefully handled before it propagates.

### Impact Explanation
An uncaught `OutOfMemoryError`, `NegativeArraySizeException`, or `ArrayIndexOutOfBoundsException` thrown deep inside precompile execution during TVM contract execution can, depending on how the surrounding `Program`/`Runtime` exception handling classifies it, either be caught as a generic VM exception (causing revert — low impact) or, in the `OutOfMemoryError` case, potentially destabilize the JVM heap for the node process, since `OutOfMemoryError` is not a checked `Exception` and can escape typical `catch (Exception e)` handlers in the actuator/runtime call stack, risking a node crash/DoS. This would qualify as a node crash/halt impact if it escapes transaction-level exception handling and propagates to block application in `Manager`.

### Likelihood Explanation
Reachable by any account via a simple contract call to the `BatchValidateSign` precompile address with crafted `data` bytes — no special privilege required. However, I could not fully verify within this session whether `OutOfMemoryError`/`ArrayIndexOutOfBoundsException` thrown here is caught by a `Throwable`-level handler further up the call stack (e.g. `Program.callToPrecompiledAddress` or `Runtime.execute`), which would downgrade this to a simple revert rather than a node-level DoS. This uncertainty is significant and I was not able to trace the full exception-handling chain before running out of investigation budget.

### Recommendation
Add explicit bounds validation of `len` against `(words.length - offset - 1)` before allocating `bytesArray`/`bytes32Array` in `extractSigArray`, `extractBytesArray`, and `extractBytes32Array`, mirroring the existing `offset > words.length - 1` guard, and reject with a `PrecompiledContractException` instead of allowing an unchecked large/negative allocation.

### Proof of Concept
Not independently reproduced. A contract call to the `BatchValidateSign` precompile with ABI-encoded `data` where the signature-array length word (at the expected `offset`) is set to a very large or negative 32-byte value would exercise `extractSigArray`'s `new byte[len][]` allocation without prior bounds checking. [1](#0-0) 

**Caveat:** I was unable to confirm the exact catch scope around the `BatchValidateSign.execute` call within the time available, so I cannot state with certainty whether this reaches a Medium-severity node-crash impact or is fully contained by existing exception handling to a mere transaction revert. This should be verified further (e.g., checking `Program.java`'s precompile invocation try/catch blocks) before treating this as a confirmed vulnerability.

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
