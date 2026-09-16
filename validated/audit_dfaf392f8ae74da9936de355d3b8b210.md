### Title
Unbounded array allocation from attacker-controlled length in `ValidateMultiSign`/`BatchValidateSign` precompiles causes node OutOfMemoryError - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The `ValidateMultiSign` (address `0x...0a`) and `BatchValidateSign` (address `0x...09`) precompiled contracts are reachable by any unprivileged account through a plain `CALL`/`STATICCALL` TVM opcode. Their execution path extracts array lengths directly from attacker-supplied calldata and immediately allocates Java arrays of that size, *before* any bound is enforced, on the legacy (pre-`allowTvmSelfdestructRestriction`) code path.

### Finding Description
`extractBytesArray` and `extractBytes32Array` read a length word straight from the caller-controlled ABI payload and allocate an array of that size with no upper bound check: [1](#0-0) 

In `ValidateMultiSign.execute`, the `MAX_SIZE` guard on `sigArraySize` is only applied when `VMConfig.allowTvmSelfdestructRestriction()` is enabled; on the legacy branch, `extractBytesArray` is invoked directly with the raw, unchecked length and only validated *after* the array has already been allocated: [2](#0-1) 

The same pattern exists in `BatchValidateSign.doExecute`, where `sigArraySize`/`addrArraySize` are only pre-validated under the same feature flag, and the unguarded `extractBytesArray`/`extractBytes32Array` calls occur otherwise: [3](#0-2) 

This is structurally identical to the reported bug class: a size value fully controlled by an untrusted caller is used to allocate memory (`new byte[len][]`) before any size limit is checked, exactly mirroring Minder's pattern of executing a user-controlled template/size into memory before bounding it. Here the "template" is the ABI length word and the "read into memory" is the `byte[len][]` allocation.

### Impact Explanation
A single crafted transaction that triggers a smart contract calling the `ValidateMultiSign` or `BatchValidateSign` precompiled contract address with a declared array length near `Integer.MAX_VALUE` (achievable because `DataWord.intValueSafe()` clamps overflow rather than rejecting the value) forces the JVM to attempt to allocate a multi-gigabyte array of references. This triggers a `java.lang.OutOfMemoryError`, which is uncaught by the surrounding logic on the un-guarded path, and can crash or destabilize the full node process — a node crash/denial-of-service, which is explicitly an accepted high-impact outcome for this class of finding.

### Likelihood Explanation
Likelihood depends on whether `allowTvmSelfdestructRestriction` (a committee-activated TVM proposal flag) is enabled on the target network. On networks/chains where this proposal has not yet been activated (e.g., private networks, test networks, or any deployment running this code before the corresponding hard fork height), the vulnerable legacy branch executes unconditionally on every call to these precompiles, making the bug trivially and repeatably triggerable by any account able to submit a `TriggerSmartContract` transaction that performs a `CALL`/`STATICCALL` to precompile address `0x9` or `0xa`.

### Recommendation
Move the `MAX_SIZE` bound check on the array-length words unconditionally before calling `extractBytesArray`/`extractBytes32Array`/`extractSigArray`, regardless of the `allowTvmSelfdestructRestriction` flag, so that the legacy code path can never allocate arrays sized directly from unchecked attacker input. Additionally, validate that the declared length is consistent with the remaining calldata length before allocating.

### Proof of Concept
1. Deploy or use any contract that performs a low-level `call`/`staticcall` to precompile address `0x000000000000000000000000000000000000000a` (`ValidateMultiSign`) or `0x0000000000000000000000000000000000000009` (`BatchValidateSign`).
2. Craft the calldata so that the length word read at `words[words[3].intValueSafe() / WORD_SIZE]` (for `ValidateMultiSign`) or the equivalent offset word for `BatchValidateSign` encodes a value close to `Integer.MAX_VALUE` (e.g., `0x7fffffff`), while the flag `allowTvmSelfdestructRestriction` is not active.
3. Submit the transaction; the node executes `extractBytesArray`/`extractBytes32Array`, attempting `new byte[0x7fffffff][]`, exhausting heap memory and throwing `OutOfMemoryError` inside VM execution — potentially destabilizing or crashing the node process serving the transaction.

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1066-1078)
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
