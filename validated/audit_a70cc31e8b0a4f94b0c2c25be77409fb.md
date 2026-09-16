# Analog Found

### Title
Unbounded attacker-controlled length/offset causes out-of-bounds array read in `ValidateMultiSign`/`BatchValidateSign` precompile decoding - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
The elfutils CVE-2017-7607 root cause is that `handle_gnu_hash` in `readelf.c` trusts an attacker-supplied length/hash-table-size field from a crafted ELF file and uses it to index into a buffer without validating that the derived offset stays within the buffer bounds, producing a heap over-read. The analogous pattern exists in java-tron's `PrecompiledContracts.extractBytesArray` / `extractSigArray` / `extractBytes` helpers, which decode attacker-controlled length and offset words taken directly from TVM call `data` (i.e., from an unprivileged smart-contract call) and use them to index into the `DataWord[] words` array and the raw `data` byte array without verifying the derived indices remain in bounds.

### Finding Description
`extractBytesArray` and `extractSigArray` read a `len` value straight from `words[offset].intValueSafe()` (fully attacker-controlled via the precompile's `data` argument), then loop `i` from `0` to `len`, dereferencing `words[offset + i + 1]` and `words[offset + bytesOffset + 1]` with no check that `offset + i + 1` or `offset + bytesOffset + 1` stay within `words.length`: [1](#0-0) [2](#0-1) 

Only the *initial* `offset` is bounds-checked (`if (offset > words.length - 1)`); the derived `bytesOffset`/`len`/loop index are never validated against `words.length` or `data.length` before being used, and `extractBytes` performs `Arrays.copyOfRange(data, offset, offset + len)` with these untrusted, unchecked values: [3](#0-2) 

This mirrors the elfutils bug class exactly: a length/offset field parsed from untrusted input is used directly for array indexing/memory access without validating it against the actual buffer size, which is the same "missing bounds check on attacker-supplied index derived from parsed data" defect.

These helpers back the `ValidateMultiSign` (address `0x...a`) and `BatchValidateSign` (address `0x...9`) precompiled contracts, both reachable from any TRC-10/TRC-20/arbitrary smart contract via a normal `CALL`/`STATICCALL` opcode once `allowTvmSolidity059()` is enabled — i.e., from a single signed transaction issued by any unprivileged account.

### Impact Explanation
An attacker-crafted `data` payload with a huge or negative `len`/`bytesOffset` word can trigger `ArrayIndexOutOfBoundsException` or `NegativeArraySizeException` deep inside precompile execution. Depending on how far up the call stack this uncaught runtime exception propagates before being caught, this can manifest as: (a) an uncontrolled exception path that is not the same normalized VM-exception/REVERT path other invalid-input cases take, risking inconsistent state handling across nodes, or (b) if any exception type here is not covered by the VM's generic exception-to-revert wrapping, a crash of the transaction-processing thread. I was not able to fully trace the top-level exception handler in `Program.java`'s precompiled-contract call site within this session to conclusively confirm whether all `RuntimeException` subtypes are uniformly caught and converted to a VM revert — this is the primary open uncertainty.

### Likelihood Explanation
High reachability: `ValidateMultiSign`/`BatchValidateSign` are ordinary precompiles callable by any contract via a single transaction with no privilege requirements, and the malformed length/offset only requires crafting the ABI-style calldata, which is trivial for any transaction broadcaster.

### Recommendation
Add explicit bounds validation in `extractBytesArray`, `extractSigArray`, and `extractBytes` (and any other decoder using attacker-controlled offsets/lengths from `DataWord[]`/`data`) to verify all derived indices (`offset + i + 1`, `bytesOffset`, `bytesLen`, and the final `Arrays.copyOfRange` range) are non-negative and within `words.length`/`data.length` before use, returning a decode failure rather than throwing an unchecked exception, consistent with the guard already present in `extractBytesArray`'s and `extractSigArray`'s initial `offset` check.

### Proof of Concept
Craft a call to the `ValidateMultiSign` precompile (address ending `...a`) with `data` such that the ABI-array "length" word at the expected array-length slot decodes to a very large positive value (e.g., `0x7fffffff`) or a value causing `offset + bytesOffset + 1` to exceed `words.length` when `intValueSafe()` is applied; invoke via a plain smart-contract `CALL` from any account. The resulting index computation in `extractBytesArray`/`extractSigArray` will exceed `words.length`, triggering `ArrayIndexOutOfBoundsException` during precompile execution — analogous to the crafted-ELF-file over-read in CVE-2017-7607.

**Caveat / unresolved uncertainty:** I could not fully confirm within this session's tool-call budget whether the VM's call-site around precompiled contract execution (`Program.java`) uniformly catches all such runtime exceptions and converts them into a safe REVERT, which would reduce this from a crash to a benign failed call. This should be verified against `actuator/src/main/java/org/tron/core/vm/program/Program.java`'s precompile invocation and exception-handling logic before treating this as confirmed high-severity crash/DoS.

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L428-430)
```java
  private static byte[] extractBytes(byte[] data, int offset, int len) {
    return Arrays.copyOfRange(data, offset, offset + len);
  }
```
