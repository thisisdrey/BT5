## Title
Uncaught length-underflow / negative-array-size exception in `ValidateMultiSign` precompile lets an attacker crash a transaction's execution path unguarded - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
CVE-2020-28194 is a variable-underflow bug: attacker-controlled length data with a value smaller than the minimum expected (< 2) is used directly to size/index a buffer without a lower-bound check, leading to memory corruption. The java-tron analog is in the `ValidateMultiSign` and its helper `extractSigArray`/`extractBytesArray` functions in `PrecompiledContracts.java`: attacker-supplied word values taken straight from TVM call data (`words[3].intValueSafe()`, `words[offset].intValueSafe()`) are used as array sizes and offsets with no bounds validation, unless the `allowTvmOsaka` feature flag's `isValidAbiEncoding` guard is active.

### Finding Description
`ValidateMultiSign.execute` decodes raw call data with `DataWord.parseArray(rawData)` and then computes: [1](#0-0) 

`words[3].intValueSafe() / WORD_SIZE` and the resulting `offset` are fully attacker-controlled and used to index into `words` and to size the signature array in `extractSigArray`/`extractBytesArray`: [2](#0-1) 

`len = words[offset].intValueSafe()` can be crafted to be negative or larger than the actual backing data, causing `new byte[len][]` to throw `NegativeArraySizeException`, or `words[offset + i + 1]`/`extractBytes` to throw `ArrayIndexOutOfBoundsException`. Only the internal permission-weight loop of `ValidateMultiSign` (lines 1082-1117) is wrapped in a `try/catch(Throwable)`; the decoding/extraction steps that call `extractSigArray`/`extractBytesArray` execute *before* that try block and are **not** guarded, so a malformed-length exception there propagates uncaught out of `execute()`.

A defensive `isValidAbiEncoding(rawData, ABI_HEADER_WORDS, ABI_ITEM_WORDS)` check exists, but it is only invoked `if (VMConfig.allowTvmOsaka())`: [3](#0-2) 
This mirrors exactly the underflow-class defect from the CVE: a length/offset field taken from untrusted input is used to compute array bounds without a minimum-size or index check, and the fix (TIP-854 style ABI validation) is feature-gated rather than universally applied.

### Impact Explanation
An unauthorized TVM caller (any contract deployer / transaction sender able to invoke the `validatemultisign` precompile at address `0x...a`) can supply calldata engineered so that `words[3]` or a nested `words[offset]` decodes to a length/offset that is negative or points outside the `words`/`data` array. This throws an unhandled Java runtime exception (`NegativeArraySizeException`/`ArrayIndexOutOfBoundsException`) from inside `PrecompiledContract.execute()`, which is not the "normal" VM exception path used elsewhere for out-of-energy/stack errors. Depending on how the outer TVM interpreter/`Program` handles unexpected `RuntimeException`s versus the dedicated `Program.Exception` types, this can either abort just the current transaction (bounded impact) or, if uncaught further up the executing thread, disrupt block/transaction processing for that node - a denial-of-service against the specific caller path, and until the exact top-level handling in `Program`/`Runtime` is confirmed, the risk of broader disruption to the node's execution pipeline cannot be ruled out.

### Likelihood Explanation
Likelihood is high for triggering the malformed-input condition: it requires only a single crafted transaction/contract call to the `validatemultisign` precompile address with hand-built calldata (no privileged role, no second party required, no waiting on chain state). The `BatchValidateSign` sibling contract already has an outer `try/catch(Throwable)` around its whole `doExecute()` (line ~1145-1153) which safely absorbs this same class of malformed-length exception and returns a benign result - `ValidateMultiSign` lacks this catch-all around the decode/extract phase, making it the exposed path.

### Recommendation
- Apply the same `isValidAbiEncoding` bounds check unconditionally in `ValidateMultiSign.execute` and `BatchValidateSign.doExecute`, not only when `VMConfig.allowTvmOsaka()` is enabled.
- Add explicit lower/upper bound validation on every length/offset value taken from `words[...]` in `extractBytesArray`, `extractSigArray`, and `extractBytes32Array` before it is used to size or index arrays (reject negative sizes and offsets/lengths beyond `data.length`).
- Wrap the entire `ValidateMultiSign.execute` body (not just the permission-weight loop) in a `try/catch` that returns `Pair.of(true, DATA_FALSE)` on any decode failure, consistent with `BatchValidateSign`'s outer catch.

### Proof of Concept
1. Call the `validatemultisign` precompile (address `0x…a`, requires `VMConfig.allowTvmSolidity059()`, which is expected to be active on production networks) from a deployed contract or via `triggerConstantContract`/`triggerSmartContract`.
2. Construct raw call data where the first three 32-byte words are arbitrary (`address`, `permissionId`, `data` pointer) and the fourth word (`words[3]`, the offset to the signature array) is set to a value such as `0xFFFFFFFF...FE` (a large/negative-after-cast value) or a small value that makes `words[offset]` (the length word inside `extractSigArray`) decode to a negative `int` via `intValueSafe()`.
3. With `VMConfig.allowTvmOsaka()` not enabled (default/pre-activation state, matching the `testTip854PreActivationNoOp` test which documents this exact pre-activation crash: `contract.execute(new byte[(5+1)*32])` throws a `RuntimeException`), `extractSigArray`/`extractBytesArray` executes `new byte[len][]` with a malformed `len`, throwing `NegativeArraySizeException`/`ArrayIndexOutOfBoundsException` that propagates out of `ValidateMultiSign.execute()` uncaught by any local handler.
4. Observe the exception surfaces past the intended VM-execution boundary instead of being converted into a graceful `false`/revert result, as confirmed by the existing repo test `ValidateMultiSignContractTest.testTip854PreActivationNoOp` (lines 248-260) which explicitly expects/catches a `RuntimeException` in the pre-activation configuration. [4](#0-3)

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1051-1074)
```java
    @Override
    public Pair<Boolean, byte[]> execute(byte[] rawData) {
      if (VMConfig.allowTvmOsaka()
          && !isValidAbiEncoding(rawData, ABI_HEADER_WORDS, ABI_ITEM_WORDS)) {
        return Pair.of(false, EMPTY_BYTE_ARRAY);
      }
      DataWord[] words = DataWord.parseArray(rawData);
      byte[] address = words[0].toTronAddress();
      int permissionId = words[1].intValueSafe();
      byte[] data = words[2].getData();

      byte[] combine = ByteUtil.merge(address, ByteArray.fromInt(permissionId), data);
      byte[] hash = Sha256Hash.hash(CommonParameter
          .getInstance().isECKeyCryptoEngine(), combine);

      if (VMConfig.allowTvmSelfdestructRestriction()) {
        int sigArraySize = words[words[3].intValueSafe() / WORD_SIZE].intValueSafe();
        if (sigArraySize > MAX_SIZE) {
          return Pair.of(true, DATA_FALSE);
        }
      }
      byte[][] signatures = VMConfig.allowTvmSelfdestructRestriction() ?
          extractSigArray(words, words[3].intValueSafe() / WORD_SIZE, rawData) :
          extractBytesArray(words, words[3].intValueSafe() / WORD_SIZE, rawData);
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
