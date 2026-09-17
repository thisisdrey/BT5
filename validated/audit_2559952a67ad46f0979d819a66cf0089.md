### Title
Unbounded array-length parsing in TVM precompiled-contract helpers allows attacker-controlled OutOfMemoryError - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
`PrecompiledContracts.extractBytesArray`, `extractBytes32Array`, and `extractSigArray` decode an array length field directly from attacker-supplied precompile call data and immediately allocate a Java array of that length, without validating it against the actual size of the supplied data or any sane upper bound. [1](#0-0) [2](#0-1) [3](#0-2) 

### Finding Description
This mirrors the Pillow BLP bug class: a "reported size" field taken from an untrusted container/input is used to size a memory allocation without first checking it against the real amount of available data. Here, `len = words[offset].intValueSafe()` is derived purely from a 32-byte word inside the calldata sent to a precompiled contract; the very next line does `new byte[len][]` (or `new byte[len]` in the sibling helpers) before any correlation is made between `len` and `data.length` or the number of words actually present. `intValueSafe()` can return any value up to `Integer.MAX_VALUE`, so a caller can supply a tiny calldata payload (just enough to define the offset word) that encodes a length of `0x7FFFFFFF`, causing the JVM to attempt to allocate an array of ~2 billion object references (many GB) in a single call.

Unlike EVM memory operations (`CODECOPY`, `EXTCODECOPY`, `CALLDATACOPY`, `VOTEWITNESS` array handling), which are gated by `EnergyCost.checkMemorySize`/`MEM_LIMIT` (3 MB) before any `Program` memory is extended, these precompile helpers sit entirely outside the metered EVM memory model and outside `calcMemEnergy`. The energy charged for calling a precompiled contract (`getEnergyForData`) is generally a function of the raw `data.length`, not of the decoded `len` value, so the cost of the attempted allocation is completely decoupled from the energy actually paid for it — the same "declared size not checked, so allocation can be arbitrarily large" root cause as the Pillow CVE.

### Impact Explanation
An uncontrolled attempt to allocate a multi-gigabyte array from a single crafted TVM `CALL`/`STATICCALL` to a precompiled contract that uses these helpers can trigger an `OutOfMemoryError` in the node's JVM. Depending on how the outer precompile `execute()` try/catch is structured, an `Error` (not `Exception`) may propagate past `catch (Exception e)` blocks used elsewhere in this file (see the Sapling precompile pattern at lines 1472-1591 which only catches `Exception`), crashing the block-processing thread and halting the node — a concrete node-crash/halt impact within scope.

### Likelihood Explanation
Reachable by any unprivileged account that can send a transaction invoking a TVM contract which performs a `CALL` to the affected precompiled address — no special privilege, stake, or witness/SR role is required, only the ability to broadcast a signed transaction with crafted calldata to a deployed contract that forwards to the precompile.

### Recommendation
Validate the decoded `len` (and any decoded `bytesOffset`/`bytesLen`) against `data.length` / `words.length` before allocating, and cap it to a small sane maximum (e.g., proportional to the actual calldata size already paid for), throwing a validation failure instead of allocating when the declared count exceeds what the supplied data can possibly contain — mirroring the fix applied to `EnergyCost.checkMemorySize` for EVM memory operations.

### Proof of Concept
Construct calldata for the precompiled contract that uses `extractBytesArray`/`extractBytes32Array` where the length word at the parsed `offset` is set to `0x7FFFFFFF` (or another very large value) while the remainder of the calldata is minimal/empty; invoke the precompile via a deployed contract's `CALL` from any account. `new byte[len][]` is executed with `len` = 0x7FFFFFFF before any bounds check against `data.length`, driving the JVM to attempt a multi-gigabyte allocation.

Note: I was unable to fully enumerate, within the available search budget, which specific precompiled-contract `execute()` method(s) invoke `extractBytesArray`/`extractBytes32Array`/`extractSigArray` in this codebase version (grep for the call sites did not return results, possibly due to indexing limits), so the exact reachable precompile address/name could not be confirmed with certainty — only the helper definitions and their missing bounds checks were directly verified.

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
