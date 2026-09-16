Based on my investigation, I found a strong analog to the OpenCV out-of-bounds read pattern (attacker-controlled length/offset field used to read outside a decoded buffer without validating it against the buffer's actual size) inside the TVM precompiled-contract signature/byte-array decoding helpers.

### Title
Unchecked attacker-controlled offset/length in precompiled-contract array decoding causes out-of-bounds array access - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
`PrecompiledContracts.extractBytesArray`, `extractSigArray`, and `extractBytes32Array` decode dynamic `bytes[]`/signature arrays from raw contract call data by reading length/offset fields directly out of the ABI-encoded `DataWord[] words` array and using them, unchecked, as indices into `words` and as byte offsets/lengths into the raw `data` buffer passed to `extractBytes`. Any account can trigger these paths by calling a TRC20/TVM precompiled contract (e.g. the multisig-validation precompiles) with crafted call data, analogous to how OpenCV's `readBlock` trusted an on-disk length field to read past the end of its buffer.

### Finding Description
`extractBytesArray` and `extractSigArray` only guard the initial `offset` against `words.length`: [1](#0-0) [2](#0-1) 

but the loop bound `len` (`words[offset].intValueSafe()`) and the per-item `bytesOffset`/`bytesLen` values are taken directly from attacker-supplied call data and are never validated against `words.length` or `data.length` before being used to index `words[offset + i + 1]`, `words[offset + bytesOffset + 1]`, or to slice `data` in `extractBytes`: [3](#0-2) 

`extractBytes32Array` has no bounds check at all on `offset`/`len` before indexing `words`: [4](#0-3) 

This is the same bug class as CVE-2017-12598: a length/offset value read from untrusted input is used to index into a buffer without first verifying it fits within the buffer's real bounds.

### Impact Explanation
A caller who invokes the affected precompiled contract (used by `ValidateMultiSign`/`BatchValidateSign`-style TVM precompiles, per `framework/src/test/java/org/tron/common/runtime/vm/ValidateMultiSignContractTest.java` and `BatchValidateSignContractTest.java`) with a crafted `len`/`bytesOffset`/`bytesLen` field can force `ArrayIndexOutOfBoundsException` or `NegativeArraySizeException` inside `extractBytesArray`/`extractSigArray`/`extractBytes32Array`. Whether this only aborts the single transaction (caught by the enclosing VM/actuator exception handling) or propagates further could not be confirmed from the available context — the test comments themselves note "this precompile has no outer catch, so a too-short input raises inside the decoder; that is the documented pre-activation failure mode," suggesting uncaught `RuntimeException`s are an accepted, pre-existing behavior for at least one of these precompiles rather than a newly introduced crash path. [5](#0-4) 

### Likelihood Explanation
High for triggering the exception: it requires only a single, unprivileged call to the precompiled contract with a malformed input — no special permissions, signatures, or timing are required, and the fields controlling the bug (`len`, `bytesOffset`, `bytesLen`) are fully attacker-controlled ABI fields.

### Recommendation
Add explicit bounds checks in `extractBytesArray`, `extractSigArray`, and `extractBytes32Array` verifying `len`, `bytesOffset`, and the resulting byte-range fit within `words.length`/`data.length` before use, mirroring the existing `verifyLength`-style checks already used elsewhere in the codebase (e.g. `framework/src/main/java/org/tron/core/capsule/utils/RLP.java`), and ensure `PrecompiledContract.execute()` failures are uniformly caught and converted into a graceful contract-call failure rather than an unhandled exception.

### Proof of Concept
Not independently verified against a running node in this session (no execution environment available). Conceptually: call the multisig-validation precompile with an ABI-encoded input whose array-length word (`len`) or an item's offset/length word is set to a large or negative value (e.g. `0xFFFFFFFF`), causing `extractBytesArray`/`extractSigArray` to compute an out-of-range index into `words` or `data` and throw `ArrayIndexOutOfBoundsException`/`NegativeArraySizeException` before any length validation occurs.

**Caveat:** I could not fully trace how this exception is handled at the `Program`/actuator layer for every precompiled contract that uses these helpers, so I cannot confirm with certainty whether the impact rises to "node crash/halt" versus a safely-reverted transaction. A Devin session with full codebase access and the ability to run the TVM test suite would be needed to determine the exact downstream exception-handling behavior and confirm severity.

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L428-430)
```java
  private static byte[] extractBytes(byte[] data, int offset, int len) {
    return Arrays.copyOfRange(data, offset, offset + len);
  }
```

**File:** framework/src/test/java/org/tron/common/runtime/vm/ValidateMultiSignContractTest.java (L244-260)
```java
  // TIP-854: before activation, malformed calldata reaches the legacy decoder.
  // Assert the guard is not taken — this precompile has no outer catch, so a
  // too-short input raises inside the decoder; that is the documented
  // pre-activation failure mode the TIP explicitly preserves.
  @Test
  public void testTip854PreActivationNoOp() {
    VMConfig.initAllowTvmOsaka(0);
    contract.setRepository(RepositoryImpl.createRoot(StoreFactory.getInstance()));
    try {
      Pair<Boolean, byte[]> ret = contract.execute(new byte[(5 + 1) * 32]);
      // If the decoder happened to handle it without raising, we must not have
      // taken the post-activation reject path (false, empty).
      Assert.assertNotSame(ByteUtil.EMPTY_BYTE_ARRAY, ret.getRight());
    } catch (RuntimeException expectedLegacyBehaviour) {
      // Pre-activation: decoder may throw — this is the existing behaviour.
    }
  }
```
