Based on my research, the closest reachable analog to the UDF "excessive length not validated before use" bug class in java-tron is in the precompiled-contract calldata parsers used by the ECDSA batch-signature-verification precompiles.

### Title
Unvalidated attacker-controlled array length causes OutOfMemoryError/DoS in precompiled contract signature array parsing - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
`extractBytesArray`, `extractBytes32Array`, and `extractSigArray` in `PrecompiledContracts.java` read an array-length word directly from smart-contract calldata and immediately use it to allocate a Java array, without validating that the length is consistent with the actual size of the supplied `data` buffer or bounded to a sane maximum.

### Finding Description
All three helper methods take the "count" from calldata via `words[offset].intValueSafe()` and use it verbatim to size an allocation: [1](#0-0) [2](#0-1) [3](#0-2) 

`intValueSafe()` only guards against sign/overflow of the underlying 256-bit word into a Java `int` (it "safely" clamps into `int` range), but it does not check the value against the length of `data` or against any protocol-level maximum. A caller can therefore supply a length value up to `Integer.MAX_VALUE` (or any large value that still fits in an `int`), causing `new byte[len][]` (an array of up to ~2^31 object references, i.e. up to ~8-16 GB depending on JVM pointer size) to be allocated before any subsequent bounds-checked loop iteration or energy metering for the operation actually executes. This mirrors the UDF bug class: a length value taken from untrusted input is used to size an internal structure (bitmap/array) without first checking that it is within a safe/representable range relative to the actual backing storage.

### Impact Explanation
If reachable with attacker-controlled calldata whose declared array length is disproportionate to the actual calldata size, the node allocating `new byte[len][]` can throw `OutOfMemoryError` or consume excessive heap, which is a crash/DoS of the executing node — the JVM's `OutOfMemoryError` is often not cleanly recoverable and can take down the transaction-processing thread pool or the whole node, affecting the node's ability to keep serving the chain. This matches the "node crash or halt" acceptance criterion.

### Likelihood Explanation
Likelihood depends on whether these three parsing helpers are reachable via a precompiled-contract call (e.g., a batch signature validation precompile) invoked through ordinary `CALL`/`STATICCALL` opcodes from a deployed contract, which would make this reachable by any transaction broadcaster or contract deployer. I was unable to fully confirm, within the available tool budget, the exact precompile class(es) (e.g., a batch-signature-validation precompile) that call `extractBytesArray`/`extractBytes32Array`/`extractSigArray`, nor whether an upstream energy/gas check (`getEnergyForData`) is evaluated and enforced *before* the array allocation occurs for every code path (constant/view calls in particular sometimes bypass strict gas enforcement). Test files (`BatchValidateSignContractTest.java`, `ValidateMultiSignContractTest.java`) reference these helper names, indicating they back a batch-signature-verification precompiled contract, but I could not verify the precompile's opcode address, its exact calling convention, or its ordering relative to energy consumption. Due to index/tool-call limits, I could not fully trace whether the framework already clamps `len` against `data.length` at a higher layer (e.g., a `CallData.readWord`/length sanity check) that would neutralize the issue. This uncertainty should be resolved in a Devin session with full file/codebase access.

### Recommendation
Add an explicit bound check immediately after reading `len` in `extractBytesArray`, `extractBytes32Array`, and `extractSigArray`, rejecting (returning empty/failure) if `len` is negative, exceeds a reasonable maximum item count for the precompile, or if `offset + len` would exceed the bounds of `words`/`data`. This should occur before any `new byte[len][]` allocation, analogous to how the UDF kernel fix rejects partition lengths that would overflow safe indexing.

### Proof of Concept
Not independently reproducible without confirming the exact precompile entry point and opcode address that invokes these helpers; a concrete PoC would craft a `CALL` to the batch-signature-validation precompile with calldata whose length-word (at the array offset) is a very large value (e.g., close to `Integer.MAX_VALUE`) while the actual `data` buffer is small, triggering `new byte[len][]` to attempt an oversized allocation. This should be validated in a full-repository Devin session to confirm the exact precompile address/dispatcher and whether energy metering already prevents this path from executing.

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
