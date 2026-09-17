### Title
Unchecked memory allocation from attacker-controlled ABI array-length words in TVM precompiled-contract calldata parsing - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
`PrecompiledContracts` contains several helper methods (`extractBytes32Array`, `extractBytesArray`, `extractSigArray`) that decode dynamic arrays embedded in the calldata passed to precompiled contracts (reached via a TVM `CALL`/`STATICCALL` to a precompiled address). These helpers read a "length" field directly out of attacker-supplied calldata via `DataWord.intValueSafe()` and immediately use it to allocate a Java array (`new byte[len][]`), with no validation that `len` is consistent with the amount of data actually supplied.

### Finding Description
`extractBytes32Array` and `extractBytesArray` take the array length straight from the decoded `DataWord` at an attacker-chosen offset and allocate an array of that size before doing any bounds check against the actual calldata size: [1](#0-0) [2](#0-1) 

`len` comes from `words[offset].intValueSafe()`, an attacker-controlled 32-byte ABI word from the precompile's `data` argument, converted to a Java `int`. There is no cap on `len` relative to `words.length` or the remaining calldata size before the allocation `new byte[len][]` executes — this is the same "read attacker length field → allocate immediately, validate later (or never)" pattern described in the gnark-crypto `Vector.ReadFrom()` advisory (unchecked `sliceLen` used directly in `make(Vector, sliceLen)`). Here, a crafted `len` near `Integer.MAX_VALUE` causes the JVM to attempt an array of ~2^31 object references (≈8–16 GB depending on JVM pointer compression) before the subsequent loop that reads `words[offset + i + 1]` ever executes and would otherwise throw an `ArrayIndexOutOfBoundsException`.

### Impact Explanation
Any account can construct and send a transaction that performs a `CALL`/`STATICCALL` from a smart contract to a precompiled-contract address whose implementation uses these helper methods with attacker-controlled offsets/lengths in calldata. Because the array size passed to `new byte[len][]` is taken directly from calldata with no upper bound, the resulting allocation can consume gigabytes of heap in a single execution step, well before any energy/CPU-time accounting for the loop body would kick in. Repeated or single large-enough calls can trigger `OutOfMemoryError` in the node process handling transaction execution/block application, degrading or crashing the validating/full node — matching the "node crash or halt" impact class accepted by this review.

### Likelihood Explanation
The precompiled-contract call path is reachable by any unprivileged account that can deploy or call into a contract performing a `CALL` to the relevant precompiled address — no special privilege, signature, or witness/SR role is required. The only barrier is TVM energy metering, but the allocation happens synchronously inside the helper before any per-element energy is charged for entries beyond the array header, so a single crafted `len` value is sufficient to trigger the outsized allocation.

### Recommendation
Before allocating `new byte[len][]` in `extractBytes32Array`, `extractBytesArray`, and `extractSigArray`, validate `len` against the actual number of `words` available (or a small, sane maximum consistent with the precompile's expected input size) and reject/short-circuit with an error result if `len` is inconsistent with the supplied calldata length, mirroring the fix pattern in `gnark-crypto` PR #759 (validate length against available data before allocating).

### Proof of Concept
1. Deploy a trivial contract that performs a `STATICCALL`/`CALL` to the precompiled address whose function invokes `extractBytes32Array`/`extractBytesArray` (e.g., a batch-signature-validation style precompile in `PrecompiledContracts`), passing calldata where the length word at the expected array-length offset is set to a very large value (e.g., `0x7FFFFFFF`) while the offset itself points within the small calldata buffer actually supplied.
2. Broadcast the transaction; execution reaches the helper method and attempts `new byte[0x7FFFFFFF][]`, forcing a multi-gigabyte allocation attempt on the executing node before any bounds check on the remaining `words` array occurs. [2](#0-1) 
3. Repeating this from multiple transactions/blocks can exhaust node heap and crash or destabilize block-processing nodes.

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
