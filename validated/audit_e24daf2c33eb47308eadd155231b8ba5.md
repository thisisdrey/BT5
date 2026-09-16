### Title
Unbounded array allocation from unvalidated calldata length in `ValidateMultiSign`/`BatchValidateSign` precompiles - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The `extractBytesArray` helper used by the `ValidateMultiSign` and `BatchValidateSign` TVM precompiled contracts reads an attacker-controlled length word from calldata and allocates a `byte[len][]` array *before* validating that `len` is bounded by the actual size of the remaining calldata. This mirrors the reported MessagePack-CSharp bug class (CWE-770: allocate from an unbounded, attacker-controlled length field prior to validating it against available payload).

### Finding Description
`extractBytesArray` in `PrecompiledContracts.java` decodes an array length directly from a `DataWord` in the caller-supplied calldata and immediately allocates an array of that size: [1](#0-0) 

```java
private static byte[][] extractBytesArray(DataWord[] words, int offset, byte[] data) {
    if (offset > words.length - 1) {
      return new byte[0][];
    }
    int len = words[offset].intValueSafe();
    byte[][] bytesArray = new byte[len][];   // allocation happens BEFORE any bound is checked
    ...
```

The only guard is `offset > words.length - 1`, which bounds the *position* of the length word, not the *value* `len` read from it. `len` comes straight from `words[offset].intValueSafe()`, fully controlled by the calling contract/transaction's ABI-encoded calldata, and can be as large as `Integer.MAX_VALUE`.

This helper is reachable from two TVM precompiles that any contract can invoke via `CALL`/`STATICCALL` to precompile addresses `0x9` (`BatchValidateSign`) and `0xa` (`ValidateMultiSign`): [2](#0-1) [3](#0-2) 

Both call sites only add a `MAX_SIZE` sanity check on the decoded length **conditionally**, gated by `VMConfig.allowTvmSelfdestructRestriction()`:

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
```

When `allowTvmSelfdestructRestriction()` is `false` (this is a governance/proposal-gated chain parameter — see `ProposalUtil.java` and `DynamicPropertiesStore.java` — so it may not be active on all networks/points in time), `extractBytesArray` is invoked directly with the raw, unvalidated `len`, allocating `new byte[len][]` with no upper bound check whatsoever before validation. The companion `MAX_SIZE` check on `signatures.length` that appears further down in each method only runs *after* the allocation has already occurred, so it cannot prevent the allocation-time failure.

The `Osaka` ABI-shape guard (`isValidAbiEncoding`) added elsewhere in these methods only validates that the *overall* calldata length is 32-byte aligned and matches a `(words - H) % I == 0` shape; it does not constrain the value of any individual length word decoded from inside the payload, so it does not prevent this issue either.

### Impact Explanation
A contract call (or a plain externally-owned-account transaction triggering a contract that calls the precompile) can supply calldata whose length-encoding word for the signature array is set to a very large value (up to `Integer.MAX_VALUE`), causing the node to attempt to allocate an oversized `byte[][]` object array during precompile execution. Because `byte[][]` is an array of object references, requesting even a moderately large `len` (tens/hundreds of millions of elements) forces a multi-gigabyte allocation attempt on the JVM heap. This can trigger `OutOfMemoryError`, degrade node performance, or crash the node process — a denial-of-service condition reachable by any account able to submit/trigger a `TriggerSmartContract` transaction that reaches the `ValidateMultiSign`/`BatchValidateSign` precompile, which is a completely unprivileged, standard entry point (any TRX holder with enough energy/fee can call it).

### Likelihood Explanation
Likelihood depends on whether `allowTvmSelfdestructRestriction` (the chain parameter gating the `MAX_SIZE` pre-check) is active. On networks/points in time where it is **not yet activated** (this is a TIP/proposal-controlled flag, historically introduced after `BatchValidateSign`/`ValidateMultiSign` themselves), any transaction can freely reach the unguarded `extractBytesArray` path with a single crafted contract call — no special privileges, staking, or governance rights required. Where the flag is active, this specific path is closed, but the same code pattern (allocate-before-validate) remains latent and could regress if the flag defaults change or the guard is bypassed in future refactors.

### Recommendation
Move the length validation ahead of allocation unconditionally in `extractBytesArray`, `extractBytes32Array`, and `extractSigArray`, independent of `VMConfig.allowTvmSelfdestructRestriction()`:
- Reject `len` values that are negative, or that are not consistent with the number of remaining words available in `words` (e.g., `offset + 1 + len > words.length`), before calling `new byte[len][]`.
- Additionally clamp `len` to the existing `MAX_SIZE` constants (5/16) unconditionally, rather than only when the feature flag is enabled, so the bound does not depend on chain-parameter activation state.

### Proof of Concept
1. Craft ABI-encoded calldata for `validatemultisign(address,uint256,bytes32,bytes[])` (precompile `0xa`) where the `bytes[]` offset word (`words[3]`) points to a length word (`words[words[3]/32]`) set to a large value, e.g. `0x7fffffff`, while the actual calldata is only a few hundred bytes long.
2. Deploy a trivial contract that forwards received calldata via `CALL` to address `0x000...a` (or `0x...9` for `BatchValidateSign`), or call the precompile directly if the node's test harness allows staticcall to that address.
3. Submit this as a `TriggerSmartContractContract` transaction (only requires normal fee/energy, no special account permissions).
4. On a node where `allowTvmSelfdestructRestriction` is not activated, `PrecompiledContracts.extractBytesArray` executes `new byte[0x7fffffff][]`, attempting a multi-gigabyte allocation, producing `OutOfMemoryError` inside the TVM execution path and potential node-wide memory pressure/crash. [1](#0-0) [4](#0-3) [5](#0-4)

### Citations

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1052-1078)
```java
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

      if (signatures.length == 0 || signatures.length > MAX_SIZE) {
        return Pair.of(true, DATA_FALSE);
      }
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
