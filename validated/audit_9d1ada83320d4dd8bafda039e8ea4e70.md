### Title
Unbounded array allocation from unvalidated length word in TVM precompile helper — potential OutOfMemoryError DoS - ([File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java])

### Summary
`PrecompiledContracts.extractBytes32Array` and `extractBytesArray` read a length value directly from attacker-controlled ABI-encoded calldata (`DataWord` words) and immediately allocate a Java array of that size, with no upper bound check on the value before allocation.

### Finding Description
`extractBytes32Array` reads `len` from `words[offset].intValueSafe()` and allocates `new byte[len][]` with no bound checking at all: [1](#0-0) 

`extractBytesArray` similarly reads `len` from an unvalidated calldata word and allocates `new byte[len][]` before any range check on `len` (only `offset` bounds are checked, not `len`): [2](#0-1) 

`DataWord.intValueSafe()` converts an arbitrary 256-bit calldata word into a 32-bit int (clamping overflow to `Integer.MAX_VALUE` rather than rejecting it), so a contract caller can freely choose `len` up to `Integer.MAX_VALUE`. This is directly analogous to CVE-2019-9704: an unchecked allocation-size value taken from untrusted input is used to size a memory allocation without verifying the allocation succeeded or that the requested size is reasonable, leading to a crash (in Java, an `OutOfMemoryError`/`NegativeArraySizeException` instead of a null-pointer dereference on a failed `calloc`).

Unlike `VerifyTransferProof.execute`, which explicitly bounds `spendCount`/`receiveCount` to `[1,2]` before allocating (`actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java:1496-1509`), these two helper methods perform no analogous bound check on the array-length word before allocation.

### Impact Explanation
If reachable from a precompiled contract that passes attacker-controlled calldata offsets/lengths into `extractBytesArray`/`extractBytes32Array` (these are generic ABI-array-decoding helpers used by precompile implementations that accept dynamic byte arrays), a transaction sender or contract deployer could trigger a very large allocation attempt (`new byte[Integer.MAX_VALUE][]`), throwing an unhandled `OutOfMemoryError` inside the shared JVM heap of the node process executing the transaction. Because the JVM heap is shared across all node threads/transactions, this can degrade or crash the node process, denying service — matching the CVE's "daemon crash" impact class.

### Likelihood Explanation
Exploitability depends on whether a precompile that is reachable via a CALL from ordinary contract code invokes these helpers with an offset/length taken straight from calldata without prior validation. I was not able to confirm the exact caller(s) of `extractBytes32Array`/`extractBytesArray` before running out of investigation budget — this needs to be verified against the callers of these two methods in `PrecompiledContracts.java` before treating this as a confirmed, remotely-triggerable crash. Given that other similar precompiles in the same file (`VerifyTransferProof`) implement explicit bound checks and note that these helper methods do not, this discrepancy is suspicious.

### Recommendation
- Add explicit upper-bound validation on `len` in both `extractBytes32Array` and `extractBytesArray` (e.g., cap to the actual remaining `words`/`data` length) before allocating, returning an empty/failure result instead of allocating an attacker-chosen size.
- Verify all callers of these two methods to confirm whether the length value is calldata-derived and unbounded at the call site as well.
- Add regression tests exercising very large length values (e.g., `Integer.MAX_VALUE`) to confirm the precompile rejects them gracefully rather than raising `OutOfMemoryError`/`NegativeArraySizeException`.

### Proof of Concept
Not independently confirmed end-to-end (caller not located in the time available). Conceptually: craft calldata to a precompile that calls `extractBytesArray`/`extractBytes32Array` such that the length word (`words[offset]`) is set to `Integer.MAX_VALUE` (or a very large value); invoking that precompile from a deployed contract would cause the node to attempt `new byte[Integer.MAX_VALUE][]`, raising `OutOfMemoryError` during transaction execution. This should be validated against the confirmed call sites in `PrecompiledContracts.java` before relying on it as proven-exploitable.

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
