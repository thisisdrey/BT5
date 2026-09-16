### Title
Unbounded array allocation/index from attacker-controlled length in ValidateMultiSign/BatchValidateSign ABI decoding helpers - ([File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java])

### Summary
The `extractBytesArray`, `extractBytes32Array` and (when `allowTvmSelfdestructRestriction` is disabled) `extractSigArray` helper functions in `PrecompiledContracts.java` read a "count" word directly from attacker-supplied calldata and use it, unchecked, both to size a Java array (`new byte[len][]`) and as a loop bound to index into the `words` array parsed from the same calldata. This mirrors the CVE-2019-18218 bug class: a `CDF_VECTOR`-style element count taken from untrusted input is used without an upper-bound check before allocation/indexing.

### Finding Description
`extractBytes32Array`, `extractBytesArray`, and `extractSigArray` all follow the same unsafe pattern: [1](#0-0) 

```
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
  ...
```

`len` comes straight from `words[offset].intValueSafe()`, i.e. a 256-bit word taken from the raw calldata of a `CALL` to the `ValidateMultiSign` (`validatemultisign(...)`) or `BatchValidateSign` (`batchvalidatesign(...)`) precompile addresses. There is no upper bound check on `len` itself before it's used to (a) allocate `new byte[len][]` and (b) index `words[offset + i + 1]` in a loop that runs `len` times.

In `BatchValidateSign.doExecute`, the size check (`sigArraySize > MAX_SIZE`) is only performed when `VMConfig.allowTvmSelfdestructRestriction()` is enabled, and even then it re-reads the same unchecked length used later by `extractSigArray`/`extractBytesArray`: [2](#0-1) 

`extractBytes32Array` (used for the `addresses` array) has **no bound check at all**, regardless of `allowTvmSelfdestructRestriction`, and no `offset > words.length - 1` guard either (unlike its siblings).

In `ValidateMultiSign.execute`, the call to `extractBytesArray`/`extractSigArray` happens *before* the surrounding `try { ... } catch (Throwable t)` block: [3](#0-2) 

so any `ArrayIndexOutOfBoundsException`, `NegativeArraySizeException`, or `OutOfMemoryError` thrown while decoding a crafted `len` propagates out of `execute()` uncaught by that precompile. The test suite explicitly documents this as a known unguarded path: "this precompile has no outer catch, so a too-short input raises inside the decoder" — [4](#0-3) .

An attacker (any contract deployer/caller) can therefore supply calldata where the `len` word for the signatures/bytes array is a huge or negative value:
- A large positive `len` (e.g., close to `Integer.MAX_VALUE`) causes `new byte[len][]` to throw `OutOfMemoryError`/huge allocation attempt, or a moderately large `len` causes `words[offset + i + 1]` to run past the bounds of the small `words` array (sized from actual calldata length), throwing `ArrayIndexOutOfBoundsException`.
- Because these exceptions are raised outside any `try/catch` in `ValidateMultiSign.execute` (and only caught defensively in `BatchValidateSign.execute` via its wrapping `try { doExecute(...) } catch (Throwable t)`), the `ValidateMultiSign` path is the more exposed one.

### Impact Explanation
This is directly reachable by any account that can send a transaction triggering a smart contract that performs a `CALL`/`STATICCALL` to the `validatemultisign` precompiled contract address with attacker-controlled calldata (no special permission required — any contract deployer or any user invoking such a contract can supply the calldata). The consequence is an uncaught runtime exception (or attempted huge/negative allocation) during precompile execution, which is a corruption of the expected sandboxing that the precompiled-contract "always returns a Pair" contract is supposed to guarantee, and which the project's own recent TIP-854 hardening effort (`isValidAbiEncoding`, `MAX_SIZE` checks, and outer-frame exception-containment tests) was specifically added to close for `BatchValidateSign`/`ValidateMultiSign`. The `ValidateMultiSign` path still has no complete equivalent (the unchecked `extractBytesArray`/`extractBytes32Array` legacy branch remains reachable when `allowTvmSelfdestructRestriction` is not enabled, and even the "protected" `extractBytes32Array` path has zero validation at all). Depending on how the propagated exception is handled by the calling VM frame and Manager block-application path, this can crash/halt node execution of that transaction's processing or bubble up unexpected exceptions during block validation, which — if it differs between nodes based on JVM heap availability — risks non-deterministic transaction execution outcomes (chain-split risk) or a denial-of-service against nodes processing the block.

### Likelihood Explanation
High likelihood of the bug being reachable: `ValidateMultiSign`/`BatchValidateSign` are TVM precompiled contracts, invokable from any contract by any address once `allowTvmSolidity059` is enabled (which it is on mainnet given the extensive test coverage and TIP-854 hardening already present). Crafting calldata with an arbitrary "array length" word at the expected offset requires no special privilege — just building a transaction with malicious raw call data, something any contract deployer/caller can do.

### Recommendation
- Add an explicit upper-bound check (e.g., against `MAX_SIZE`) on the `len` value read in `extractBytesArray`, `extractBytes32Array`, and `extractSigArray` themselves, unconditionally, rather than relying on call-site checks gated behind `allowTvmSelfdestructRestriction`.
- Validate `len >= 0` and `offset + len` does not exceed `words.length` before allocation/indexing in all three helpers.
- Wrap `ValidateMultiSign.execute` similarly to `BatchValidateSign.execute` with a top-level `try/catch(Throwable)` that safely returns `(true, DATA_FALSE)` on any decoding failure, consistent with the TIP-854 containment guarantee already tested for `BatchValidateSign`.

### Proof of Concept
Craft calldata for a `CALL` to the `validatemultisign` precompiled contract address (`0000000000000000000000000000000000000000000000000000000000000101` equivalent constant) where the ABI-encoded dynamic array length word (at the offset pointed to by the `bytes[]` parameter's dynamic offset) is set to a very large value, e.g. `0x7fffffff`, while the total calldata itself is short (few words). Calling `ValidateMultiSign.execute(data)` (or via the TVM `CALL` opcode from a deployed contract) with `allowTvmSelfdestructRestriction` disabled drives execution into `extractBytesArray`, where `new byte[0x7fffffff][]` triggers `OutOfMemoryError`/allocation failure, or a moderately large `len` value (bigger than `words.length`) drives `words[offset + i + 1]` to throw `ArrayIndexOutOfBoundsException` — both uncaught by `ValidateMultiSign.execute`, consistent with the documented pre-activation failure mode in `ValidateMultiSignContractTest.testTip854PreActivationNoOp` [5](#0-4) .

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1066-1082)
```java
      if (VMConfig.allowTvmSelfdestructRestriction()) {
        int sigArraySize = words[words[3].intValueSafe() / WORD_SIZE].intValueSafe();
        if (sigArraySize > MAX_SIZE) {
          return Pair.of(true, DATA_FALSE);
        }
      }
      byte[][] signatures = VMConfig.allowTvmSelfdestructRestriction() ?
          extractSigArray(words, words[3].intValueSafe() / WORD_SIZE, rawData) :
          extractBytesArray(words, words[3].intValueSafe() / WORD_SIZE, rawData);

      if (signatures.length == 0 || signatures.length > MAX_SIZE) {
        return Pair.of(true, DATA_FALSE);
      }

      AccountCapsule account = this.getDeposit().getAccount(address);
      if (account != null) {
        try {
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1156-1181)
```java
    private Pair<Boolean, byte[]> doExecute(byte[] data)
        throws InterruptedException, ExecutionException {
      if (VMConfig.allowTvmOsaka()
          && !isValidAbiEncoding(data, ABI_HEADER_WORDS, ABI_ITEM_WORDS)) {
        return Pair.of(false, EMPTY_BYTE_ARRAY);
      }
      DataWord[] words = DataWord.parseArray(data);
      byte[] hash = words[0].getData();

      if (VMConfig.allowTvmSelfdestructRestriction()) {
        int sigArraySize = words[words[1].intValueSafe() / WORD_SIZE].intValueSafe();
        int addrArraySize = words[words[2].intValueSafe() / WORD_SIZE].intValueSafe();
        if (sigArraySize > MAX_SIZE || addrArraySize > MAX_SIZE) {
          return Pair.of(true, DATA_FALSE);
        }
      }

      byte[][] signatures = VMConfig.allowTvmSelfdestructRestriction() ?
          extractSigArray(words, words[1].intValueSafe() / WORD_SIZE, data) :
          extractBytesArray(words, words[1].intValueSafe() / WORD_SIZE, data);
      byte[][] addresses = extractBytes32Array(
          words, words[2].intValueSafe() / WORD_SIZE);
      int cnt = signatures.length;
      if (cnt == 0 || cnt > MAX_SIZE || signatures.length != addresses.length) {
        return Pair.of(true, DATA_FALSE);
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
