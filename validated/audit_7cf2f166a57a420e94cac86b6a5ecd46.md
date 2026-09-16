### Title
Unbounded array allocation from attacker-controlled length field in TVM precompile ABI decoding helpers - ([File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java])

### Summary
`PrecompiledContracts.extractBytes32Array`, `extractBytesArray`, and `extractSigArray` decode ABI‑encoded arrays passed to the `batchValidateSign` / `validateMultiSign` (and signature-array) precompiled contracts by reading a length word directly from attacker-supplied calldata and immediately allocating a Java array of that size, with no upper bound check against the actual calldata size or a configured memory/energy limit — the same bug class as the ImageMagick advisory: a length value taken from untrusted input is used to size an allocation before any check against the allowed/available memory.

### Finding Description
`extractBytes32Array` reads `len = words[offset].intValueSafe()` and immediately does `byte[][] bytes32Array = new byte[len][]` before validating `len` against the number of words actually present in the calldata: [1](#0-0) 

`extractBytesArray` and `extractSigArray` follow the identical pattern — `len` is parsed straight from a `DataWord` under attacker control, and a `byte[len][]` array is allocated before any bytes are actually copied or bounds‑checked: [2](#0-1) 

`intValueSafe()` only clamps the value to the `int` range (up to `Integer.MAX_VALUE`), it does not check it against the true size of the `words`/`data` array supplied to the precompile. Because these are pointer/reference arrays (`byte[][]`), a length close to `Integer.MAX_VALUE` causes the JVM to attempt to allocate on the order of tens of gigabytes for the array of references alone — long before the loop that copies real bytes (and would fail with an index error) ever executes. This mirrors the ImageMagick flaw: a length/size field taken from a small, attacker-crafted input is used to drive an allocation that is never checked against a real memory-allocation ceiling before the allocation happens.

These helper functions are used to decode signature/byte arrays for the `batchValidateSign` and `validateMultiSign` precompiled contracts, which are reachable by any contract executing a `CALL`/`STATICCALL` to the fixed precompile addresses once `VMConfig.allowTvmSolidity059()` is enabled: [3](#0-2) 

The precompile's `getEnergyForData` cost is a small fixed/linear energy charge unrelated to the actual heap memory an attacker can force the JVM to try to allocate, so energy metering does not prevent the oversized allocation attempt.

### Impact Explanation
An `OutOfMemoryError` thrown mid-allocation inside precompile execution can destabilize the whole node JVM (heap fragmentation/exhaustion affecting all other threads, not just the executing transaction), leading to node crash or degraded service — matching the "node crash or halt" / "no longer serve API" impact bar for this scan. This is directly analogous to the ImageMagick advisory's denial-of-service via unchecked allocation size.

### Likelihood Explanation
Any account can deploy a contract that calls `batchValidateSign` or `validateMultiSign` with crafted calldata containing an oversized length word at the expected offset; no special privilege, stake, or witness/SR role is required — only a signed transaction invoking the precompile. The trigger is a single crafted transaction.

### Recommendation
Validate the decoded `len` (and any per-item offset/length values) in `extractBytes32Array`, `extractBytesArray`, and `extractSigArray` against the actual size of `words`/`data` (or a hard maximum consistent with calldata size limits) before allocating the destination array, rejecting the call (return failure) instead of allocating when the value is inconsistent with the supplied input.

### Proof of Concept
Deploy a minimal contract or send a raw transaction that performs a low-level `call` to the `validateMultiSign`/`batchValidateSign` precompile address, with calldata engineered so that the word at the array-length `offset` used by `extractBytesArray`/`extractSigArray` is set to a large value (e.g., `0x7FFFFFFF`) while the rest of the calldata is minimal. This is not something I can execute in this read-only environment; a background Devin session with a running node would be needed to confirm the resulting `OutOfMemoryError`/node impact empirically.

**Note:** I could not fully trace the exact byte offsets used by `batchValidateSign`/`validateMultiSign` when calling these three helper methods (the call sites were not retrieved before the tool budget ran out), so the precise calldata layout needed for a working exploit transaction is not confirmed — only the vulnerable allocation pattern in the shared decoding helpers is verified from source.

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
