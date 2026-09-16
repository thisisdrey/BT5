## Finding: Unbounded array allocation from attacker-controlled length field in TVM precompiled-contract input parsers

### Title
Unvalidated length field drives unbounded `byte[][]` allocation in TVM precompiled contracts, causing node OOM/crash — (`File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The ImageMagick bug is a classic "attacker-controlled length field used to allocate memory before any bound/consistency check" pattern (CWE-770/CWE-789). The same pattern exists in java-tron's precompiled-contract input decoders `extractBytesArray`, `extractBytes32Array`, and `extractSigArray`, which read a length word directly out of the call-data words and immediately allocate an array sized by that value, before validating it against the actual size of the supplied `data`.

### Finding Description
`extractBytes32Array`, `extractBytesArray`, and `extractSigArray` in `PrecompiledContracts.java` take the element count `len` straight from the caller-supplied words and use it to size a new array with no upper-bound check against the real length of `data`: [1](#0-0) 

```
390:  private static byte[][] extractBytes32Array(DataWord[] words, int offset) {
391:    int len = words[offset].intValueSafe();
392:    byte[][] bytes32Array = new byte[len][];
...
399:  private static byte[][] extractBytesArray(DataWord[] words, int offset, byte[] data) {
400:    if (offset > words.length - 1) {
401:      return new byte[0][];
402:    }
403:    int len = words[offset].intValueSafe();
404:    byte[][] bytesArray = new byte[len][];
```

`intValueSafe()` clamps a 256-bit `DataWord` value only to the `int` range (i.e. up to `Integer.MAX_VALUE`), it does not clamp to anything related to the actual size of `data`/`words`. A contract call whose input encodes a huge `len` (e.g. `0x7FFFFFFF`) causes the JVM to attempt to allocate a `byte[2147483647][]` reference array (~17 GB of pointer slots on a 64-bit JVM before even populating any inner `byte[]`), which will throw `OutOfMemoryError` well before the subsequent loop that would otherwise fail on out-of-bounds access. This mirrors the ImageMagick SVG bug: a size value taken from attacker input is used to drive an allocation with no sanity check tied to the real payload size.

`extractSigArray` has the identical pattern: [2](#0-1) 

### Impact Explanation
Any account can trigger this by deploying/calling a contract that invokes the precompiled contract(s) that use these helpers, supplying a crafted `len` word in the call data. The resulting `OutOfMemoryError` is thrown inside TVM execution on the full node processing the transaction. Depending on how the node's exception handling treats `OutOfMemoryError` versus catchable VM exceptions (`Program.OutOfMemoryException` is a distinct, deliberately-thrown and caught exception type used elsewhere for memory-limit enforcement — see `Program.Exception.memoryOverflow` and `EnergyCost.checkMemorySize` guarding `Memory`/opcode-level allocations), an uncaught JVM-level `OutOfMemoryError` from this direct array allocation can crash or destabilize the node process handling the broadcast transaction, which is a denial-of-service against the node (and, if it hits enough nodes at consensus time, can affect block production availability).

### Likelihood Explanation
The trigger requires only a single crafted transaction calling the vulnerable precompiled contract with an oversized length field in its input; no special privileges, keys, or ordering are needed. The main uncertainty is which exact precompiled contract addresses route through `extractBytesArray`/`extractBytes32Array`/`extractSigArray` in this build (they support batch/multi-signature and array-oriented precompiles) — I could not fully enumerate all call sites within the available tool budget, so likelihood should be validated against the concrete precompile dispatch table (`getContractForAddress`) before treating this as fully confirmed exploitable in production configuration.

### Recommendation
- Before allocating, bound `len` against a small hard cap (e.g., a small fixed maximum element count consistent with the precompile's expected input shape) and/or against `(data.length - offset*32)/32`, rejecting the call with `Pair.of(false, EMPTY_BYTE_ARRAY)` if `len` is unreasonable — mirroring the pattern already used in `ModExp.execute` (`baseLen > UPPER_BOUND` checks) at [3](#0-2) .
- Apply the same fix to all three helper methods (`extractBytes32Array`, `extractBytesArray`, `extractSigArray`).

### Proof of Concept
Conceptual PoC (requires confirming the concrete precompile address that calls these helpers):
1. Deploy/trigger a contract call whose `data` targets the vulnerable precompile.
2. Encode the length word at the expected `offset` as `0x7FFFFFFF` (or another value large enough to exceed available heap when multiplied by pointer size), with the remainder of `data` short/absent.
3. Submit the transaction; the precompile's `execute(byte[] data)` invokes `extractBytesArray`/`extractBytes32Array`/`extractSigArray`, which executes `new byte[len][]` and throws `OutOfMemoryError` inside the node's TVM execution path, before any energy/length-consistency check can reject the malformed input.

Note: I was unable to conclusively trace, within available tool calls, exactly which precompiled contract address(es) call these three helper methods and whether `OutOfMemoryError` here is caught by an outer TVM exception handler (which would downgrade impact to "wasted CPU/heap churn" rather than a crash). This should be verified against `PrecompiledContracts.getContractForAddress` and the Program's top-level exception handling around precompile `execute()` before treating this as a confirmed, exploitable High severity issue.

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L697-700)
```java
      if (VMConfig.allowTvmOsaka()
          && (baseLen > UPPER_BOUND || expLen > UPPER_BOUND || modLen > UPPER_BOUND)) {
        return Pair.of(false, EMPTY_BYTE_ARRAY);
      }
```
