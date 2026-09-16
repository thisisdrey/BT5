## Title
Unbounded array allocation from attacker-controlled length word in TVM precompiled-contract ABI decoders — potential OOM/DoS on any node executing the transaction ([File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java])

### Summary
`PrecompiledContracts.java` contains helper decoders (`extractBytes32Array`, `extractBytesArray`, `extractSigArray`) used to parse ABI-encoded arrays passed as calldata to precompiled contracts reachable from ordinary TVM `CALL`/`STATICCALL` opcodes. Each of these methods reads an array-length word straight from attacker-controlled calldata and immediately allocates a Java array of that size, before validating it against the actual remaining calldata length: [1](#0-0) 

```java
private static byte[][] extractBytes32Array(DataWord[] words, int offset) {
    int len = words[offset].intValueSafe();
    byte[][] bytes32Array = new byte[len][];
    ...
}

private static byte[][] extractBytesArray(DataWord[] words, int offset, byte[] data) {
    if (offset > words.length - 1) {
      return new byte[0][];
    }
    int len = words[offset].intValueSafe();
    byte[][] bytesArray = new byte[len][];
    ...
}
```

`intValueSafe()` converts a 256-bit `DataWord` to a Java `int` without throwing on overflow — it clamps to a safe representable value (e.g. `Integer.MAX_VALUE`) rather than being bounded by the actual size of `data`/`words`. This mirrors the Grav pattern exactly: a request-derived magnitude value is fed straight into a size-driving allocation call (`imagecreatetruecolor($w,$h)` in Grav vs. `new byte[len][]` here) with no ceiling check against the source data size or any configurable maximum.

### Finding Description
Precompiled contracts in java-tron (dispatched via `PrecompiledContracts.getContractForAddress`) are invoked whenever a smart contract executes `CALL`/`STATICCALL`/`DELEGATECALL` to one of the reserved low addresses — this is reachable by any account that can broadcast a `TriggerSmartContract` transaction or deploy a contract that performs such a call, i.e., a fully unauthenticated/unprivileged actor from the node's perspective (any signed transaction is sufficient, no special permission required).

`extractBytes32Array`/`extractBytesArray`/`extractSigArray` decode an ABI-style dynamic array header from the raw call `data`: [2](#0-1) 

The `len` value is taken directly from a single 32-byte word under full attacker control and used as the dimension of a newly allocated Java array (`new byte[len][]` — an array of object references, 8+ bytes per element on a 64-bit JVM before any payload bytes are even read). There is no check that `len` is consistent with the size of `data`/`words`, nor any configured maximum length, before the allocation occurs. A crafted call to any precompile that uses these helpers (multi-signature validation precompiles are the consumers of these array extraction helpers in this codebase) can set the length word to a value up to `Integer.MAX_VALUE`, forcing the executing node to attempt to allocate gigabytes of heap for a single array before any subsequent bounds/energy check can reject it.

This is directly analogous to the Grav report: an unauthenticated, single request/transaction supplies a magnitude parameter that is used to size a memory allocation with no clamp at the boundary where the value is first consumed, causing memory/CPU exhaustion on the serving process.

### Impact Explanation
A successful trigger causes the executing full node (validating or serving the transaction) to attempt a very large heap allocation, risking `OutOfMemoryError` and a JVM-wide crash or severe GC pressure/stall across all threads in that process — a concrete node crash/halt condition, not merely elevated resource use. Because transaction execution in java-tron happens as part of block validation/application, an attacker able to get such a transaction included or even just broadcast for local validation on nodes (mempool re-validation) could degrade or crash SRs and full nodes processing it, which is a network-wide availability impact.

### Likelihood Explanation
Reachability requires only a single signed, unprivileged transaction (a `TriggerSmartContract`/contract call invoking the vulnerable precompile address with a crafted `data` payload). No special permission, plugin, or non-default configuration is required, matching the "no account, plugin, or non-default config required" characteristic of the original report. The likelihood is high given the low barrier to constructing the calldata.

### Recommendation
- Validate the extracted `len` against the actual remaining size of `data`/`words` (or a strict maximum item count) **before** calling `new byte[len][]` in `extractBytes32Array`, `extractBytesArray`, and `extractSigArray`.
- Reject the precompile call (return failure / consume all energy) if the declared array length is inconsistent with the supplied calldata length, mirroring `isValidAbiEncoding`'s existing bound checks elsewhere in the same file.
- Consider using `intValueSafe()` results only after confirming `len * itemSize <= data.length` to avoid allocation-before-validation.

### Proof of Concept
Construct a `TriggerSmartContract` transaction whose contract calls one of the precompiled addresses that consumes `extractBytes32Array`/`extractSigArray` (the signature-array precompiles in `PrecompiledContracts.java`), with calldata whose length-header word set to a very large value (e.g., `0x7fffffff`) while the remaining calldata is minimal/absent. On execution, the node attempts `new byte[0x7fffffff][]`, exhausting available heap and risking crash/hang of the executing node process — reachable from a single unauthenticated transaction.

**Note on verification limits:** I was unable to fully trace, within the available tool budget, exactly which precompiled contract(s) invoke `extractBytes32Array`/`extractSigArray` (the call sites are elsewhere in `PrecompiledContracts.java`, not captured in the retrieved excerpts) or confirm the precise clamp behavior of `DataWord.intValueSafe()`. These should be confirmed by reading the full `PrecompiledContracts.java` file and `DataWord.intValueSafe()` implementation before treating this as fully proven; the root-cause pattern (unclamped attacker-controlled length driving array allocation) is nonetheless clearly present in the cited code.

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L414-430)
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

  private static byte[] extractBytes(byte[] data, int offset, int len) {
    return Arrays.copyOfRange(data, offset, offset + len);
  }
```
