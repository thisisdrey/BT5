Based on my investigation, I found a concrete analog in the TVM precompiled-contract input parsing code, which is directly reachable by any contract deployer/caller (analogous to GPAC's `avc_parse_slice` trusting attacker-controlled length/index fields from untrusted media data).

### Title
Unvalidated attacker-controlled length/offset fields in precompile ABI array extraction can cause out-of-bounds array access - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
`extractBytesArray`, `extractSigArray`, and `extractBytes32Array` in `PrecompiledContracts.java` read a `len` value directly from attacker-supplied `DataWord[] words` (decoded straight from the raw calldata to a precompiled contract) and then loop `offset + i + 1` into the `words` array and `extractBytes(data, ...)` into the raw `data` byte array without validating that `len`, `bytesOffset`, or `bytesLen` are within bounds of the source arrays.

### Finding Description
`extractBytes32Array` reads `len = words[offset].intValueSafe()` and then indexes `words[offset + i + 1]` for `i` up to `len` [1](#0-0) . `extractBytesArray` similarly derives `len`, `bytesOffset`, and `bytesLen` purely from attacker data and calls `extractBytes(data, (bytesOffset + offset + 2) * WORD_SIZE, bytesLen)`, which does `Arrays.copyOfRange(data, offset, offset + len)` with no bounds validation [2](#0-1) . `extractSigArray` has the identical pattern [3](#0-2) . This mirrors the CVE-2023-23143 bug class: a length/index field taken from untrusted input is used to drive array reads/copies without verifying it stays inside the buffer bounds.

`extractBytesArray` only guards the initial `offset` (`if (offset > words.length - 1) return new byte[0][];`) but does not bound-check the loop index `offset + i + 1` against `words.length`, nor `bytesOffset`/`bytesLen` against `data.length`, before calling `Arrays.copyOfRange`.

### Impact Explanation
If reachable with an oversized or negative `len`/`bytesOffset`/`bytesLen`, this throws unhandled `ArrayIndexOutOfBoundsException` or `NegativeArraySizeException`/`OutOfMemoryError` (for very large `bytesLen`) inside `Arrays.copyOfRange`. If this exception is not caught by the surrounding TVM precompile dispatch/execution try-catch, it could crash the node process handling the transaction (denial of service on block application), rather than remaining a contained revert.

### Likelihood Explanation
I could not confirm, within the available index, exactly which precompiled contract(s) invoke `extractBytesArray`/`extractSigArray`/`extractBytes32Array` at the top level, nor whether TVM's precompile execution wraps these calls in a catch-all `Throwable` handler that would turn any such exception into a normal revert instead of propagating up and crashing block processing. This is essential to determine actual exploitability and severity, and I was unable to verify it with the tools available (only 8 usage matches were found, all within `PrecompiledContracts.java` itself, and I ran out of iterations before locating the top-level `execute()` methods and their exception handling in the enclosing precompile-dispatch code).

### Recommendation
Add explicit bounds checks in `extractBytesArray`, `extractSigArray`, and `extractBytes32Array` validating that `len >= 0`, `offset + i + 1 < words.length`, and that `bytesOffset`/`bytesLen` keep all reads within `data.length`, throwing a handled exception (or returning empty arrays) rather than allowing raw `Arrays.copyOfRange`/array indexing to fail unpredictably.

### Proof of Concept
I cannot construct a concrete, verified PoC transaction because I was unable to confirm (a) which specific precompiled contract address dispatches to these extraction helpers, and (b) whether an enclosing try/catch in the precompile execution path already contains the exception. Without confirming these two points, I cannot assert this rises to a validated High/Critical finding — this should be verified in a live/local java-tron node by crafting a `TriggerSmartContract` call to the relevant precompile with a crafted array length word before treating this as confirmed.

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L399-430)
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

  private static byte[] extractBytes(byte[] data, int offset, int len) {
    return Arrays.copyOfRange(data, offset, offset + len);
  }
```
