### Title
Unbounded array-length field drives excessive memory allocation in TVM precompiled-contract signature helpers - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
The `extractBytes32Array`, `extractBytesArray`, and `extractSigArray` helpers in `PrecompiledContracts.java` read an array-length value directly from attacker-controlled calldata words and immediately use it to allocate a Java array (`new byte[len][]`) before validating that `len` is consistent with the actual size of the supplied `data`/`words` buffer. This mirrors the CVE-2019-6966 pattern in Bento4's `AP4_ElstAtom`/`AP4_Array::EnsureCapacity`, where a length field taken from untrusted input is used to size an allocation before the value is checked against the real amount of available data.

### Finding Description
`extractBytes32Array` reads `len = words[offset].intValueSafe()` and immediately allocates `new byte[len][]` with no upper bound check against the actual length of `words`: [1](#0-0) 

`extractBytesArray` and `extractSigArray` have the identical pattern: they only bound-check the starting `offset`, then read `len` from a single attacker-controlled word and allocate `new byte[len][...]` immediately, with the loop that would actually touch `words`/`data` out-of-bounds occurring only after the allocation: [2](#0-1) 

These helper methods are private utility functions of `PrecompiledContracts`, used to parse dynamic `bytes[]`/`bytes32[]` ABI-encoded parameters for the multi-signature validation precompiled contracts (`ValidateMultiSign` / `BatchValidateSign`), which are reachable from any TVM `CALL`/`STATICCALL` to those precompile addresses — i.e. from any deployed smart contract invoked by an unprivileged transaction. Because `intValueSafe()` clamps to the full `int` range (up to `Integer.MAX_VALUE`), an attacker can craft calldata whose length word is a very large positive integer, causing an attempted allocation of billions of array-slot references (`new byte[Integer.MAX_VALUE][]`) before any bounds validation occurs.

### Impact Explanation
A successful trigger causes a large/failed allocation attempt inside the TVM execution path invoked by the precompiled contract dispatcher. Depending on JVM heap headroom this can throw `OutOfMemoryError`, which is not a checked exception and may propagate past the normal TVM revert-and-continue handling used for arithmetic/logic errors, risking a node crash or halted block processing — matching the "node crash or halt" impact bar for this analog category.

### Likelihood Explanation
Any account can deploy a trivial contract that calls the affected precompiled contract with crafted calldata and broadcast a single transaction to trigger the code path; no privileged role, other honest party, or specific chain state is required, making this trivially and repeatedly reachable by an anonymous contract caller.

### Recommendation
Before allocating with `new byte[len][...]`, validate `len` against a sane upper bound and against the actual remaining size of `words`/`data` (e.g., re-use the existing `isValidAbiEncoding` bound-checking pattern already used elsewhere in the file), rejecting the call gracefully instead of allocating an array sized directly from untrusted input.

### Proof of Concept
Deploy a contract that performs a `CALL`/`STATICCALL` to the `ValidateMultiSign`/`BatchValidateSign` precompiled address with calldata engineered so the length word read by `extractBytes32Array`/`extractBytesArray`/`extractSigArray` (at the offset consumed as the array-count field) is set to a very large positive 32-byte integer (e.g., `0x7fffffff`), while the remainder of the calldata is arbitrary/short. Broadcasting this transaction drives execution into the vulnerable helper, which allocates `new byte[0x7fffffff][]` prior to any bounds check against the real calldata size.

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
