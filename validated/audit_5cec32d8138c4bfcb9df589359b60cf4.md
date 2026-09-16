## Analog Found: Unbounded Array Allocation in TVM Precompiled Contract Signature/Bytes Array Parsing

### Title
Unvalidated attacker-controlled length used to allocate arrays in TVM precompile helpers causes excessive memory allocation / DoS - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The CVE describes `Exiv2::Jp2Image::readMetadata` allocating memory based on an unvalidated, attacker-supplied size field read from a crafted file, causing excessive memory allocation. The analogous pattern in java-tron is in the precompiled-contract call-data parsing helpers `extractBytes32Array`, `extractBytesArray`, and `extractSigArray`, which read a "count/length" word directly out of the ABI-encoded call data and immediately allocate a Java array of that size before any bound check against the actual size of the supplied data.

### Finding Description
`extractBytes32Array`, `extractBytesArray`, and `extractSigArray` all follow the same pattern: they take a `len` value straight from the caller-supplied `words` array (itself derived from the raw `data` passed into a precompiled contract's `execute(byte[] data)`), and use it to size a Java array before validating that `len` is sane relative to the remaining `words`/`data` length: [1](#0-0) [2](#0-1) [3](#0-2) 

In all three methods, `int len = words[offset].intValueSafe();` is read from attacker-controlled ABI-encoded data and used directly as the size for `new byte[len][]` (or, in the inner loop of `extractBytesArray`, as the size for a subsequent `Arrays.copyOfRange` call in `extractBytes`). None of these methods clamp `len` against the actual number of remaining `words`, nor against any sane maximum item count. `extractBytesArray` and `extractSigArray` do check `offset > words.length - 1` for the offset itself, but perform no check that `len` (the *count* of subsequent items) is consistent with the actual size of `words`/`data`. `extractBytes32Array` doesn't even guard the offset.

This mirrors the root cause of CVE-2018-4868: a length field taken from untrusted, attacker-controlled input is used to size a memory allocation without validating it against the bounds of the actual available data.

### Impact Explanation
Because these helpers back precompiled-contract call handling in the TVM (i.e., reachable from any Solidity contract executing a `CALL`/`STATICCALL` to a precompile address with attacker-crafted call data), a malicious contract deployer or caller can supply a very large `len` value (bounded only by `DataWord`'s `intValueSafe()` conversion, which can return values up to `Integer.MAX_VALUE`). This triggers allocation of a huge `byte[][]` array, which can throw an `OutOfMemoryError` inside the node process executing the transaction, potentially destabilizing or crashing the SR/full node — a "node crash or halt" impact as defined in scope.

### Likelihood Explanation
The precompiled-contract entry points (`execute(byte[] data)`) are invoked as part of ordinary TVM contract execution for any transaction that calls the relevant precompile address, meaning the trigger is reachable by any unprivileged contract deployer/caller with no special privileges — only the ability to broadcast a transaction that calls a contract invoking the affected precompile. However, I was **not able to fully confirm within the available tool budget** which specific precompile(s) call `extractBytes32Array`/`extractBytesArray`/`extractSigArray` (they appear used for multi-signature/multi-bytes precompiles based on naming and `SIG_LENGTH` usage), nor did I confirm the exact clamping behavior of `DataWord.intValueSafe()` (I could not view its implementation before running out of iterations). This uncertainty should be verified before treating this as fully confirmed.

### Recommendation
Add explicit bounds validation before allocation in all three helper methods: verify `len >= 0` and that `offset + 1 + len <= words.length` (and, for `extractBytesArray`/`extractSigArray`, that computed `bytesOffset`/`bytesLen` stay within the bounds of `data`) before calling `new byte[len][]` or `Arrays.copyOfRange`. Reject with an exception/early return (as already done for the `offset > words.length - 1` case) rather than allocating first and validating later.

### Proof of Concept
Not independently constructed/executed — this assessment is based on static code reading of `PrecompiledContracts.java`. A concrete PoC would require crafting ABI-encoded call data for whichever precompile invokes `extractBytesArray`/`extractSigArray` with an inflated leading length word (e.g., `0x7FFFFFFF`) and confirming that `DataWord.intValueSafe()` propagates that large value into the `new byte[len][]` allocation, which I could not confirm before the tool-call budget was exhausted.

**Caveat:** Due to the exhaustion of available tool iterations, I could not verify (1) the exact caller precompile(s)/contract addresses that invoke these three helper methods, or (2) the precise clamping semantics of `DataWord.intValueSafe()`. If further certainty is needed, a Devin session with full repository access should inspect `DataWord.intValueSafe()` in `common/src/main/java/org/tron/common/runtime/vm/DataWord.java` and trace all call sites of `extractBytes32Array`, `extractBytesArray`, and `extractSigArray` in `PrecompiledContracts.java` to confirm reachability and the maximum attacker-controllable `len` value.

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
