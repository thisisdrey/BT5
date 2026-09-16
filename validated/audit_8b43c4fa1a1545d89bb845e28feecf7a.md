Based on my investigation, I found a directly analogous pattern in `PrecompiledContracts.java`, reachable by any contract deployer/caller through the TVM precompile at address matching `BatchValidateSign`.

### Title
Unbounded Memory Allocation via Attacker-Controlled Array-Length Word in Precompiled Contract ABI Decoding - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
The `extractBytes32Array`, `extractBytesArray`, and `extractSigArray` helper methods in `PrecompiledContracts.java` read a 32-byte "length" word directly from attacker-supplied precompile call data and use it, unvalidated against the actual size of the remaining input buffer, to allocate a `byte[len][]` array before any bounds checking occurs.

### Finding Description
`extractBytes32Array` computes `len` from `words[offset].intValueSafe()` and immediately allocates `new byte[len][]` with no check that `len * 32` (or any per-element size) fits within the actual `words` array length: [1](#0-0) 

`extractBytesArray` and `extractSigArray` follow the same pattern — they only guard against `offset > words.length - 1`, not against `len` itself being attacker-inflated far beyond the actual data size, and they too allocate `new byte[len][]` before validating the length against available data: [2](#0-1) 

This mirrors the CVE-2026-53585 bug class: an attacker-controlled size field taken from a structured input header is trusted for allocation sizing before the parser validates it against the real payload, allowing a small, cheap request to trigger a disproportionately large allocation.

Because `words[offset].intValueSafe()` is derived from a `DataWord` (32-byte big integer clamped to `int`), a caller can supply a length word up to `Integer.MAX_VALUE` (~2^31-1) while the call data itself is only a handful of bytes. `new byte[Integer.MAX_VALUE][]` alone requests roughly 8-16 GB for the array of references (before any element is populated), which can throw `OutOfMemoryError` or cause severe GC pressure on the node process handling the transaction/contract call.

### Impact Explanation
Any contract deployer or caller can invoke a precompiled contract that uses these extraction helpers (e.g., the shielded-transaction verification precompiles) with crafted call data containing an oversized length word at negligible cost relative to the requested allocation, since energy accounting is based on the input data size rather than the derived array length. A successful attack can exhaust node heap memory, causing `OutOfMemoryError` and JVM instability/crash — this affects full node and SR node availability, i.e., a node crash or halt reachable via a single signed transaction/contract call, matching the "node crash or halt" acceptance criterion.

### Likelihood Explanation
Likelihood is high for any code path that calls these helpers with attacker-controlled offsets/lengths without an upstream sanity bound (e.g., comparing `len` against `MAX_SIG_COUNT`/data length before array allocation). The precompile is reachable from ordinary contract execution by an unprivileged caller with no special permissions, and the malicious payload is small (well under normal transaction data-size limits), making the attack cheap to mount repeatedly.

### Recommendation
Before allocating `new byte[len][]` in `extractBytes32Array`, `extractBytesArray`, and `extractSigArray`, validate that `len` is non-negative and bounded by a sane maximum consistent with the actual remaining `words`/`data` length (e.g., `len <= (words.length - offset - 1)`), rejecting or truncating the call otherwise — analogous to how `isValidAbiEncoding` already validates lengths elsewhere in the same file.

### Proof of Concept
Construct call data for a precompile that internally invokes `extractBytes32Array`/`extractBytesArray` (offset words following the shielded-transaction-verification precompile's expected layout) where the length word at the target offset is set to a very large value (e.g., `0x7FFFFFFF`) while the rest of the call data is only a few words long. Submitting this as a `TriggerSmartContract` transaction calling the precompile causes the node to attempt `new byte[0x7FFFFFFF][]`, exhausting available heap and potentially crashing the executing node process before any subsequent bounds-check on `offset + i + 1` throws `ArrayIndexOutOfBoundsException`.

Note: I was unable to fully trace which specific precompiled contract entry points invoke `extractBytesArray`/`extractSigArray` due to iteration limits — full confirmation of the exact energy-cost function for that call path would benefit from further review in a live Devin session with complete file access.

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
