Based on my research, the closest reachable analog to CVE-2025-66960 (unvalidated length field read from untrusted input used to size an allocation, causing a DoS panic) in java-tron is the unbounded length extraction in `PrecompiledContracts.extractBytes32Array` and `extractBytesArray`.

### Title
Unvalidated length field from precompiled-contract call data allows unbounded array allocation / DoS - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
`extractBytes32Array` and `extractBytesArray` read a 32-byte word from attacker-supplied TVM call data, convert it to an `int` via `intValueSafe()`, and immediately use that value as an array size (`new byte[len][]`) with no upper-bound sanity check against the actual size of `data`/`words`, mirroring the GGUF bug class where `readGGUFV1String` trusts an attacker-controlled length field before allocating/reading.

### Finding Description
`extractBytes32Array` computes `len` directly from `words[offset].intValueSafe()` and allocates `new byte[len][]` before validating that `len` is consistent with the remaining `words` array length: [1](#0-0) 

`extractBytesArray` has the same pattern — `len` is taken from calldata with only a check that `offset` is in range, not that `len` is bounded, before allocating `new byte[len][]` and then looping `len` times to derive further offsets/lengths from calldata: [2](#0-1) 

Because these words come directly from TVM call data supplied via a contract call (an unprivileged, permissionless input path), an attacker can set the length word to a very large value (up to `Integer.MAX_VALUE`), forcing the node to attempt to allocate an oversized `byte[][]`. This is directly analogous to the GGUF bug where `readGGUFV1String` reads a length from untrusted metadata and uses it to size a subsequent read without bounds validation, leading to a crash/DoS.

### Impact Explanation
If the allocation is large enough, it will throw `OutOfMemoryError`. Unlike checked exceptions that some precompiled contracts wrap in `catch (Throwable any)` blocks (e.g., the ECRecover and VerifyTransferProof code paths I reviewed at lines 608-623 and 1472-1541 of the same file, which do use broad `Throwable` catches), I was not able to confirm from the indexed code whether the precompiled contract(s) that call `extractBytes32Array`/`extractBytesArray` wrap their invocation in an equivalent broad catch. If they do not, an `OutOfMemoryError` thrown mid-array-construction during block/transaction execution could propagate up through the VM interpreter loop and destabilize or crash the node process — a denial-of-service condition reachable by any account submitting a transaction that calls the affected precompiled contract.

### Likelihood Explanation
The precompiled contracts reachable through `PrecompiledContracts.getContractForAddress` are invoked from ordinary TVM `CALL` opcodes, meaning any contract deployer or caller can construct calldata with an attacker-chosen length word at the relevant offset. No special privilege is required — only a signed transaction that calls the specific precompiled contract that internally exercises `extractBytes32Array`/`extractBytesArray` with untrusted lengths.

### Recommendation
Add an explicit upper bound check on `len` in both `extractBytes32Array` and `extractBytesArray` (e.g., validating `len` against the remaining length of `words` or a hard-coded maximum count) before allocating the output array, consistent with the bounds checks already present in `RLP.calcLength`/`verifyLength` and `PrecompiledContracts.VerifyTransferProof.execute` (spendCount/receiveCount bounds), so the code rejects malformed lengths before attempting the allocation.

### Proof of Concept
Because I could not locate (within the indexed portion of the codebase) the exact precompiled contract entry point that calls `extractBytesArray`/`extractBytes32Array` with attacker-controlled offsets end-to-end, and could not confirm whether that call site is wrapped in a `catch (Throwable)` that would neutralize an `OutOfMemoryError`, I cannot construct a fully verified PoC transaction. This is a limitation of my current investigation, not a confirmation that the path is safe or unsafe — a Devin session with full repository access would be needed to trace the concrete caller(s) of `extractBytesArray`/`extractBytes32Array`, confirm whether their `execute()` methods catch `Throwable`/`OutOfMemoryError`, and build a concrete calldata PoC. [3](#0-2)

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
