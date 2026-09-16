### Title
Unbounded Array Allocation from Attacker-Controlled Length in PrecompiledContracts ABI Array Extraction - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
`PrecompiledContracts.extractBytes32Array` and `PrecompiledContracts.extractBytesArray` read an array-length value directly out of caller-supplied precompile calldata and immediately allocate a Java array of that size (`new byte[len][]`) *before* validating that `len` is consistent with the actual size of the input data. This mirrors the ImageSharp CWE-770/CWE-789 pattern: a size field taken from untrusted input is used to drive a memory allocation with no upper bound or cross-check against the real payload size.

### Finding Description
In `extractBytes32Array`: [1](#0-0) 
`len` comes straight from `words[offset].intValueSafe()`, a 256-bit value taken from the precompile call's input data and safely narrowed to an `int`. The code allocates `new byte[len][]` and then loops `len` times reading `words[offset + i + 1]` — with no check that `offset + i + 1` stays within `words.length`, and no check that `len` is bounded by the actual input size.

`extractBytesArray` has the same pattern, only checking that the *offset* is in range, not that `len` is bounded: [2](#0-1) 

Because `len` is attacker-controlled (any value up to `Integer.MAX_VALUE` after `intValueSafe()` clamping) and is used directly as an array-allocation size, a caller can craft a precompile call whose data encodes a huge declared array length while the actual data buffer is tiny. The JVM will attempt to allocate an array of up to ~2^31 references (many GB of heap) before the subsequent loop ever gets a chance to fail with an `ArrayIndexOutOfBoundsException`. This is the same root cause class as the reported ImageSharp bug: a length/size value is trusted and used for allocation prior to validating it against the real payload.

### Impact Explanation
Any unprivileged account or contract can trigger this by calling one of the TRON precompiled contracts (in the zk-SNARK / Sapling / signature-batch precompile family in this file) that invokes `extractBytes32Array`/`extractBytesArray`, with a calldata field whose length word is set to a very large value. This can force the node executing the transaction (both the block-producing node and every full/validating node re-executing the block) to attempt a massive heap allocation, resulting in `OutOfMemoryError` and process instability — a denial-of-service against transaction processing/block application, consistent with "node crash or halt."

### Likelihood Explanation
The precompiled-contract execute path is reachable directly from any signed transaction that calls the contract address associated with these precompiles (no special privilege required), so likelihood of triggering the code path is high. What is **not verified** (index limits prevented locating the exact precompile dispatch/energy-cost sites and callers of these two helpers) is: (a) which specific precompiled contract(s) invoke `extractBytes32Array`/`extractBytesArray`, (b) whether `getEnergyForData` for those specific precompiles charges energy proportional to `len` before `execute()` runs, which could mitigate this by making the attack economically costly, and (c) the exact behavior of `DataWord.intValueSafe()` for out-of-range values (need to confirm it doesn't already clamp to something small, e.g. the actual data length). These call sites could not be located via search in this pass, so likelihood should be treated as **unconfirmed at the "reachable and economically viable" level** even though the code pattern itself is confirmed vulnerable-looking.

### Recommendation
Before allocating `new byte[len][]` in both `extractBytes32Array` and `extractBytesArray`, validate that `len` is bounded both by a sane maximum and by the actual remaining size of `words`/`data` (e.g., `len <= (words.length - offset - 1)`), rejecting the precompile call (returning failure) otherwise — mirroring the existing bounds check already present for `offset` in `extractBytesArray`.

### Proof of Concept
Not fully constructible without confirming the exact precompile entry point(s) and gas metering for these two helpers (blocked by index coverage limits on caller sites). Conceptually: craft calldata to the affected precompiled contract where the length word at the expected array-length slot is set to a very large value (e.g., `0x7FFFFFFF`) while the remaining calldata is minimal; submitting this via a normal TVM `CALL`/`STATICCALL` to the precompile address would drive the JVM to allocate an oversized array in `extractBytes32Array`/`extractBytesArray`.

---
**Note on completeness**: due to index size limits, I could not locate the concrete call sites within `PrecompiledContracts.java` that invoke `extractBytes32Array`/`extractBytesArray` (to identify the exact precompile/opcode and its energy-cost function), nor the full `intValueSafe()` implementation. A Devin session with full repository access would be needed to confirm the energy-cost gating and finalize a working PoC before treating this as a confirmed exploitable finding rather than a strong code-pattern match.

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
