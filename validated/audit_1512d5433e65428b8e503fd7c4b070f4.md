### Title
Unbounded array allocation in ValidateMultiSign/BatchValidateSign precompiled contracts allows memory-exhaustion DoS - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The TVM precompiled contracts `ValidateMultiSign` (address `0xa`) and `BatchValidateSign` (address `0x9`) decode attacker-controlled ABI-encoded calldata and allocate Java arrays whose sizes are taken directly from an embedded length word in that calldata, without validating the length against a sane upper bound or the actual size of the remaining payload. This mirrors the GoFlow sflow decoder bug (CVE-2022-2529): a length field parsed from untrusted input is used to size a memory allocation before the data backing that length is validated, letting a remote/unprivileged party trigger a large memory allocation with a small crafted message.

### Finding Description
`extractBytesArray`, `extractBytes32Array`, and `extractSigArray` read a length word from the decoded `DataWord[]` array and immediately use it to allocate a 2D byte array: [1](#0-0) 

These helpers are invoked from `ValidateMultiSign.execute` and `BatchValidateSign.doExecute`, which are reachable by any contract call to precompile addresses `0xa` and `0x9`: [2](#0-1) [3](#0-2) 

The size upper-bound guard (`sigArraySize > MAX_SIZE` / `addrArraySize > MAX_SIZE`) is only applied when the `VMConfig.allowTvmSelfdestructRestriction()` feature flag — a committee-proposal-gated switch — is active: [4](#0-3) [5](#0-4) 

When the flag is not active (default/legacy state, or on chains/testnets where the proposal has not yet been activated), `extractBytesArray`/`extractBytes32Array` are called with an attacker-chosen `len` derived from `words[offset].intValueSafe()` with no relation to the real payload size, and no cap is applied before `new byte[len][]` is allocated. The separate `isValidAbiEncoding` check (gated by `VMConfig.allowTvmOsaka()`) only validates that the *overall* calldata length is consistent with a fixed header/item word shape; it does not bound an individually-embedded length field like the one read at `words[offset]`, so it does not prevent this from being reached with an arbitrarily large `len`.

This is structurally identical to the GoFlow bug: a length taken from untrusted, remotely-supplied data is used to size an allocation without adequate sanitization, and the guard that would prevent it (`MAX_SIZE` check) exists but is conditionally bypassed.

### Impact Explanation
Any account able to broadcast a transaction that triggers a contract `CALL` to precompile address `0x9` or `0xa` with crafted calldata can force the executing node to attempt a very large array allocation (`new byte[len][]`), consuming large amounts of heap almost instantly. Because energy/CPU metering in `getEnergyForData` is computed from `data.length` (cheap to satisfy with a small buffer while `len` itself is an independent 256-bit word extracted from inside that buffer), the energy cost paid by the caller does not scale with the memory actually requested. Repeated or single large calls can trigger `OutOfMemoryError` in the node process, causing a crash or degraded service — a node-crash/halt condition, which is in the accepted impact set for this analysis.

### Likelihood Explanation
Reachable via a plain smart-contract call from any account (no special privileges), using standard ABI encoding of `batchvalidatesign(bytes32,bytes[],address[])` or `validatemultisign(...)`. The vulnerable code path is taken whenever `VMConfig.allowTvmSelfdestructRestriction()` is not active, which is the pre-activation/default configuration; exploitability therefore depends on the runtime configuration of the target chain rather than any additional attacker capability.

### Recommendation
Move the `MAX_SIZE` bound check (and a bound relative to the actual remaining `data`/`words` length) so it is applied unconditionally in `extractBytesArray`, `extractBytes32Array`, and `extractSigArray`, before allocating any array, rather than gating it behind `VMConfig.allowTvmSelfdestructRestriction()`. Additionally validate that `offset`, `len`, and any computed `bytesOffset`/`bytesLen` stay within the bounds of `words`/`data` prior to allocation, independent of feature-flag activation state.

### Proof of Concept
1. Craft calldata for `batchvalidatesign(bytes32,bytes[],address[])` (or `validatemultisign`) where the ABI offset word for the `bytes[]` parameter points to a length word set to a very large value (e.g. close to `Integer.MAX_VALUE`), while the actual physical calldata buffer is kept small.
2. Deploy a trivial contract that performs a low-level `call` to precompile address `0x0000...09` (BatchValidateSign) or `0x0000...0a` (ValidateMultiSign) with that calldata, or invoke it directly via `TriggerSmartContract`.
3. On a node where `VMConfig.allowTvmSelfdestructRestriction()` is not active, `extractBytesArray`/`extractBytes32Array` executes `new byte[len][]` with the attacker-controlled `len`, causing the node’s JVM to attempt a massive allocation and throw `OutOfMemoryError`, which is only caught generically in `BatchValidateSign.execute`’s outer try/catch (mapping to a zero result) but the transient memory spike and GC pressure it causes can still degrade or crash the node process, especially under repeated or concurrent invocation.

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1156-1177)
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
```
