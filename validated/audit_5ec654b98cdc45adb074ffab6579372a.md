### Title
Unbounded array allocation from attacker-controlled length word in `ValidateMultiSign`/`BatchValidateSign` precompiles allows heap-exhaustion crash - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The Alpine advisory describes a heap overflow in Perl's `DBI` because a "count" field taken directly from user input (number of SQL placeholders) is used to size an internal buffer without a hard, pre-allocation limit. `java-tron`'s `PrecompiledContracts.extractBytesArray()` / `extractBytes32Array()` / `extractSigArray()` have the same bug-class shape: they read a length word straight out of attacker-controlled `CALL` data and immediately allocate a Java array of that size, and only *afterwards* (and only when a config flag is enabled) do the callers check that the length is within the intended `MAX_SIZE` bound. [1](#0-0) 

### Finding Description
`extractBytes32Array` and `extractBytesArray` both do:
```java
int len = words[offset].intValueSafe();
byte[][] bytesArray = new byte[len][];
```
`words[offset]` is a `DataWord` parsed straight out of the raw `CALL` payload supplied by the caller (`DataWord.parseArray(data)`), so `len` is fully attacker-controlled up to whatever `intValueSafe()` converts a 256‑bit value to (used elsewhere in the codebase specifically to avoid overflow exceptions when handling untrusted stack/calldata words). [2](#0-1) 

In `ValidateMultiSign.execute()` (and identically in `BatchValidateSign.doExecute()`), the `MAX_SIZE` (5 for `ValidateMultiSign`, 16 for `BatchValidateSign`) sanity check on the length is only performed when `VMConfig.allowTvmSelfdestructRestriction()` is enabled, and even then it is applied to `extractSigArray`'s path, not to the legacy `extractBytesArray`/`extractBytes32Array` path used otherwise:
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
``` [3](#0-2) 

When the legacy branch is taken, `extractBytesArray`/`extractBytes32Array` executes `new byte[len][]` with an unvalidated, attacker-supplied `len` *before* any `MAX_SIZE` bound is enforced — mirroring the CVE's core defect of allocating memory proportional to an untrusted "count" without a pre-allocation cap. The same unguarded pattern exists in `BatchValidateSign`, which additionally calls `extractBytes32Array(words, ...)` unconditionally (no `allowTvmSelfdestructRestriction` guard at all for that call): [4](#0-3) 

A newer guard, `isValidAbiEncoding`, bounds the overall calldata length/shape but only when `VMConfig.allowTvmOsaka()` is enabled, and it validates calldata *shape* (multiple-of-word alignment), not that the embedded length word `len` is small — it does not stop a well-formed, word-aligned payload from encoding an enormous `len` value in the length slot. [5](#0-4) 

### Impact Explanation
An unprivileged transaction sender or contract caller can invoke the `ValidateMultiSign` (address `0x...a`) or `BatchValidateSign` (address `0x...9`) precompile via a normal `CALL`/`STATICCALL`/`TriggerSmartContract` with a crafted `bytes[]` argument whose length-prefix word encodes a very large integer. This causes the node to attempt allocating a huge `byte[][]` (`new byte[len][]`), which can throw `OutOfMemoryError` or otherwise place severe memory pressure on the JVM heap shared by all validating/witness nodes processing that transaction. Because this triggers during transaction execution (which every full node and SR/witness must replay identically), a single crafted transaction can crash or destabilize node processes network-wide, which is a "node crash or halt" impact class explicitly in scope.

### Likelihood Explanation
The precompiles are reachable by any account able to broadcast a `TriggerSmartContract` transaction that performs a low-level `CALL` to precompile addresses `0x9`/`0xa` — no special privilege, contract deployment, or witness/SR role is required. Exploitability depends on the runtime configuration state: the vulnerable, unguarded path (`extractBytesArray`/`extractBytes32Array` without the `MAX_SIZE` pre-check) is taken whenever `VMConfig.allowTvmSelfdestructRestriction()` is not yet active for a given chain/parameter set, and even when active, `extractBytes32Array`'s length in `BatchValidateSign` is not gated by that same flag at all, so the allocation-before-validation pattern remains present.

### Recommendation
- In `extractBytesArray`, `extractBytes32Array`, and `extractSigArray`, validate `len` against the caller's `MAX_SIZE` (and against the remaining `words.length`) **before** allocating any array, unconditionally (not only when `allowTvmSelfdestructRestriction()` is enabled).
- Ensure `BatchValidateSign`'s call to `extractBytes32Array` is bounded by the same `MAX_SIZE` pre-check applied to its `extractSigArray`/`extractBytesArray` counterpart.
- Consider making `isValidAbiEncoding`-style structural validation (or an explicit length cap check) unconditional rather than gated behind `VMConfig.allowTvmOsaka()`/`allowTvmSelfdestructRestriction()`, since bounding attacker-controlled allocation sizes should not depend on unrelated feature flags.

### Proof of Concept
1. Craft calldata for `ValidateMultiSign` (`address,uint256,bytes32,bytes[]`) where the offset for the `bytes[]` array points to a length word set to a very large value (e.g., `0x7FFFFFFF`), while `VMConfig.allowTvmSelfdestructRestriction()` is not yet enabled (legacy path) as covered by the existing `testTip854PreActivationNoOp` test showing the pre-activation legacy decoder path is reachable. [6](#0-5) 
2. Submit a `TriggerSmartContract` transaction whose bytecode performs `CALL` to precompile address `0x...a` with this calldata.
3. During `ValidateMultiSign.execute()`, `extractBytesArray(words, offset, rawData)` executes `new byte[len][]` with `len` derived from the crafted word, immediately attempting a multi-gigabyte allocation and triggering `OutOfMemoryError`/heap exhaustion in the node processing the transaction. [7](#0-6)

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L432-438)
```java
  private static boolean isValidAbiEncoding(byte[] data, int headerWords, int itemWords) {
    if (data == null || data.length % WORD_SIZE != 0) {
      return false;
    }
    long tail = subtractExact(data.length, multiplyExact(headerWords, WORD_SIZE));
    return tail > 0 && tail % multiplyExact(itemWords, WORD_SIZE) == 0;
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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1165-1177)
```java
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
