### Title
Unbounded array allocation from attacker-controlled length field in TVM precompiles `BatchValidateSign`/`ValidateMultiSign` causes OOM/DoS - (`actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
`extractBytes32Array` and `extractBytesArray` in `PrecompiledContracts.java` allocate a Java array whose size is taken directly from a length word inside the precompile calldata, before any bound is enforced. This is the same bug class as CVE‑2021‑35516 (Apache Commons Compress 7z): a length field read from untrusted input is used to size a memory allocation prior to validating it against a sane maximum, allowing a small malicious input to trigger a huge allocation attempt.

### Finding Description
`extractBytes32Array` allocates `new byte[len][]` where `len` comes straight from the calldata (`words[offset].intValueSafe()`), with no upper bound check: [1](#0-0) 

`extractBytesArray` similarly allocates `new byte[len][]` from an attacker-controlled `len`: [2](#0-1) 

Both are invoked from `BatchValidateSign.doExecute` (address `0x9`) and `ValidateMultiSign.execute` (address `0xa`), which are TVM precompiled contracts reachable by any contract that issues a `CALL`/`STATICCALL` to those addresses: [3](#0-2) [4](#0-3) 

Critically, the size cap (`MAX_SIZE`, 16 for `BatchValidateSign`, 5 for `ValidateMultiSign`) is only checked **before** allocation when `VMConfig.allowTvmSelfdestructRestriction()` is enabled: [5](#0-4) 
The post-allocation `cnt > MAX_SIZE` check (line 1179 for `BatchValidateSign`, line 1076 for `ValidateMultiSign`) happens only *after* `extractBytes32Array`/`extractBytesArray` has already attempted the allocation — so on chains/paths where `allowTvmSelfdestructRestriction` is not active, or simply because allocation happens unconditionally before the guard, a length value up to `Integer.MAX_VALUE` (bounded only by `DataWord.intValueSafe()`) is used directly as the array dimension.

### Impact Explanation
A crafted, attacker-controlled `bytes[]`/`address[]` length word inside calldata to the `batchvalidatesign` or `validatemultisign` precompile causes the JVM to attempt allocating an array with up to ~2^31-1 elements (each a reference, ~8-16 bytes with array header overhead), i.e. tens of gigabytes, immediately throwing `OutOfMemoryError`. Because these precompiles are executed inside contract execution during normal block/transaction processing (including validating and re-executing incoming transactions), repeated exploitation can degrade or crash node JVMs processing the block, producing a node crash/halt — a network-reachable denial-of-service analogous to the reported CVE.

### Likelihood Explanation
Any account can deploy a trivial contract that performs a raw `CALL` to precompile address `0x9` or `0xa` with attacker-chosen calldata and then invoke it via a normal `TriggerSmartContractContract` transaction — no special privileges are required. The vulnerable extraction functions are reached directly from calldata-derived offsets with no length sanity check before allocation on the affected path.

### Recommendation
- Enforce the `MAX_SIZE` (and any other sane upper bound, e.g. bounded by remaining calldata length / `WORD_SIZE`) check on the length value **before** calling `extractBytes32Array`/`extractBytesArray`/`extractSigArray`, unconditionally (not gated behind `VMConfig.allowTvmSelfdestructRestriction()`).
- Additionally validate that `len * WORD_SIZE` does not exceed the actual `data.length`/`words.length` before allocating, mirroring the `verifyLength` pattern already used in `RLP.java`.

### Proof of Concept
1. Construct ABI-encoded calldata for `batchvalidatesign(bytes32,bytes[],address[])` (or `validatemultisign`) where the length word for the `address[]`/`bytes[]` array (read at the offset computed from `words[2].intValueSafe()`/`words[3].intValueSafe()`) is set to a very large value (e.g. `0x7FFFFFFF`) instead of the real number of following elements.
2. Deploy a minimal contract that forwards this calldata via `CALL` to precompile address `0x0000...09` (BatchValidateSign) or `0x0000...0a` (ValidateMultiSign).
3. Trigger the contract via a normal signed `TriggerSmartContractContract` transaction on a node where `allowTvmSelfdestructRestriction` is not enabled (or before the guard's allocation-order issue is fixed).
4. Observe `extractBytes32Array`/`extractBytesArray` attempting `new byte[0x7FFFFFFF][]`, throwing `OutOfMemoryError` / causing severe GC pressure on the executing node, before the post-allocation `MAX_SIZE` check at line 1179/1076 is ever reached.

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1066-1074)
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
