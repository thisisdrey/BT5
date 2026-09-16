### Title
Unbounded array-length field from precompile call data allocates attacker-controlled array size, causing OOM crash - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The precompiled-contract helper methods `extractBytes32Array`, `extractBytesArray`, and `extractSigArray` in `PrecompiledContracts.java` read a length value directly from attacker-supplied `DataWord[] words` (decoded from the TVM `CALL`/`STATICCALL` input data to a precompile) and immediately use it to size a Java array allocation (`new byte[len][]`), without validating that `len` is consistent with the actual size of `words` or `data`. This is structurally the same bug class as CVE-2020-15117: a length field taken from untrusted input is used to drive memory allocation before it is validated against the bounds of the actual payload, allowing a remote/unprivileged caller to trigger excessive memory allocation and crash the process.

### Finding Description
`extractBytes32Array` computes `len` from `words[offset].intValueSafe()` and then allocates `new byte[len][]` with no check that `len` is smaller than `words.length - offset` or any other sane upper bound: [1](#0-0) 

`extractBytesArray` and `extractSigArray` follow the same pattern — they only guard against `offset > words.length - 1`, but the derived `len` (also taken from attacker-controlled word data) is used unchecked to allocate `bytesArray` and to compute `bytesOffset`/`bytesLen` for subsequent reads: [2](#0-1) 

These helpers are invoked from TVM precompiled-contract implementations that parse the raw call data of a `CALL`/`STATICCALL` to a precompile address (e.g., the multi-signature/permission-validation precompiles), which any deployed smart contract can invoke with attacker-chosen `data`. An attacker crafts a small input payload whose "array length" word (one 32-byte ABI slot) is set to a very large value; the code allocates `new byte[len][]` proportional to that value, causing a large heap allocation attempt that can throw `OutOfMemoryError` or exhaust node memory — directly analogous to Synergy's `kMsgHelloBack` handler trusting an attacker-supplied `0xffffffff` length field to allocate memory before validating it against the actual packet size.

### Impact Explanation
A contract invoking these precompiles with a maliciously crafted length word can force the executing full node to attempt a huge array allocation, resulting in an `OutOfMemoryError` / crash of the node process executing the transaction (or, depending on JVM heap sizing, severe GC pressure and service degradation for the node). Because block validation and re-execution of the same transaction occur on all nodes processing the block, a successful crash-inducing transaction can disrupt more than one node, materially impacting availability of the network's transaction-processing/query paths — matching the "node crash/halt" impact bucket allowed by the rules.

### Likelihood Explanation
Likelihood is limited by the fact that: (1) TVM energy metering charges for precompile execution (the caller must have enough fee/energy for the call), and (2) it was not confirmed in this investigation whether `DataWord.intValueSafe()` clamps out-of-range values to a small bound (e.g., `Integer.MAX_VALUE`) or throws before reaching the allocation, which would reduce or negate exploitability. Without being able to fully inspect `intValueSafe()`'s implementation, I can only confirm that the array-length value taken from attacker input is not checked against the actual size of the `words`/`data` buffer before being used to size an allocation. If `intValueSafe()` allows a moderately large value to pass through unclamped, the allocation cost is far cheaper (in energy) than the memory footprint it produces, making this a plausible low-cost DoS.

### Recommendation
Add an explicit bound check on `len` immediately after extraction in `extractBytes32Array`, `extractBytesArray`, and `extractSigArray` — reject when `len` exceeds `words.length - offset - 1` (the maximum number of elements that could possibly be present in the supplied `words` array) or any other protocol-defined maximum array size, before performing the `new byte[len][]` allocation. This mirrors the fix applied upstream in Synergy (validating the declared length against the actually available buffer size before allocating).

### Proof of Concept
1. Deploy or call an existing contract that invokes one of the TVM precompiles relying on `extractBytes32Array`/`extractBytesArray`/`extractSigArray` (e.g., a multi-signature validation precompile) via `CALL`/`STATICCALL`.
2. Craft the ABI-encoded call data so that the "array length" word at the relevant `offset` decodes (through `DataWord.intValueSafe()`) to a very large integer.
3. Submit the transaction; the precompile helper executes `new byte[len][]` with the attacker-chosen `len`, forcing the JVM to attempt a correspondingly large allocation.
4. Observe `OutOfMemoryError` / node instability during transaction execution. [3](#0-2)

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L390-426)
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
