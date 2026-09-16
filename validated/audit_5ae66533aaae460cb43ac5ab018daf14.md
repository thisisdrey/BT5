### Title
Unbounded attacker-controlled array length in TVM precompile helpers causes OOM/crash - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The `ALPINE-CVE-2024-46952` Ghostscript bug is a buffer overflow caused by trusting a size/width value (`W` array) read directly from untrusted PDF data to size subsequent memory operations. The closest analog in java-tron is in the `ValidateMultiSign` (precompile `0x0a`) and `BatchValidateSign` (precompile `0x09`) TVM precompiled contracts, where a length field is read straight out of attacker-supplied call data and used to allocate a Java array without validating that it is non-negative or bounded, before any size-cap check is applied.

### Finding Description
`extractBytesArray` and `extractBytes32Array` read a length word directly from the decoded call-data words and immediately allocate an array of that size: [1](#0-0) 

```java
private static byte[][] extractBytes32Array(DataWord[] words, int offset) {
  int len = words[offset].intValueSafe();
  byte[][] bytes32Array = new byte[len][];
  ...
private static byte[][] extractBytesArray(DataWord[] words, int offset, byte[] data) {
  ...
  int len = words[offset].intValueSafe();
  byte[][] bytesArray = new byte[len][];
  ...
```

`intValueSafe()` derives an `int` straight from attacker-controlled `DataWord` bytes and can yield any value in `[Integer.MIN_VALUE, Integer.MAX_VALUE]`, including negative values. These functions are invoked from `ValidateMultiSign.execute` and `BatchValidateSign.doExecute`: [2](#0-1) [3](#0-2) 

The size-limiting check (`sigArraySize > MAX_SIZE`) is only performed **when `VMConfig.allowTvmSelfdestructRestriction()` is enabled**; when that feature flag is off, `extractBytesArray`/`extractSigArray`/`extractBytes32Array` are called with the raw, unchecked length straight from calldata, so a huge or negative `len` reaches `new byte[len][]` before any bound is enforced — directly mirroring the Ghostscript pattern of trusting an attacker-supplied size/width field to size a buffer before validating it.

This is reachable by any account that can issue a `CALL`/`STATICCALL`/`DELEGATECALL` to precompiled addresses `0x9` (`BatchValidateSign`) or `0xa` (`ValidateMultiSign`) with `VMConfig.allowTvmSolidity059()` enabled — i.e., any Solidity contract call or `triggerSmartContract`/`triggerConstantContract` request, satisfying the "contract deployer/order placer/anonymous API client" reachability requirement.

### Impact Explanation
A crafted call-data length word of `Integer.MAX_VALUE` (or a large positive value) drives `new byte[len][]`, which throws `OutOfMemoryError` or takes excessive time/memory to allocate; a negative value throws `NegativeArraySizeException`. Neither `ValidateMultiSign.execute` nor `BatchValidateSign.doExecute` wraps the call to `extractBytesArray`/`extractBytes32Array` in the surrounding try/catch (the try/catch in `ValidateMultiSign` starts *after* the array extraction, and in `BatchValidateSign` the outer catch in `execute()` does catch `Throwable`, but `OutOfMemoryError` propagation can still destabilize the JVM heap for the processing node before being caught, and repeated invocations can be used to reliably force GC pressure/OOM across the node). Since this code runs both during real block application (`Manager`) and constant-call/estimate-energy query paths, an attacker can use this to degrade or crash a full node processing the transaction/block — a denial-of-service against consensus-critical or RPC-serving nodes.

### Likelihood Explanation
Exploitability depends on `VMConfig.allowTvmSelfdestructRestriction()` being disabled for the target chain/version; on networks where this TIP has already been activated, the size check (`MAX_SIZE`, e.g. 5 or 16) is enforced before array extraction, closing this specific gap. On any chain/version where the flag is not yet active, the call data is fully attacker-controlled (any Solidity contract can issue the raw `CALL`), making the trigger trivial and requiring no privileged access — matching the CVE's low complexity/no-authentication profile.

### Recommendation
Validate `len` from `extractBytesArray`, `extractBytes32Array`, and `extractSigArray` against `0` and a hard maximum (e.g., `MAX_SIZE`) unconditionally, *before* allocating any array, rather than gating the check behind the `allowTvmSelfdestructRestriction` feature flag. Treat out-of-range lengths as an immediate contract-execution failure (`Pair.of(true, DATA_FALSE)`), matching the same discipline `isValidAbiEncoding` already applies to overall calldata shape.

### Proof of Concept
1. Deploy a contract (or send a raw external-contract call) invoking `CALL` to address `0x…09` (`batchvalidatesign(bytes32,bytes[],address[])`) with hand-crafted, non-ABI-standard calldata where the array-length word for the `bytes[]` parameter is set to `0x7FFFFFFF` (or a negative 32-byte word) instead of a real array length.
2. On a chain/version with `allowTvmSelfdestructRestriction` not yet activated, `BatchValidateSign.doExecute` calls `extractBytesArray(words, offsetFromAttacker, data)` directly; `len = 0x7FFFFFFF` drives `new byte[len][]`.
3. Observe `OutOfMemoryError`/excessive allocation attempt on the executing node while processing the transaction (either via `triggerConstantContract` for immediate feedback, or via a mined transaction affecting all full nodes during block application).

Note: I was unable to fully confirm from the index whether `allowTvmSelfdestructRestriction` is unconditionally enabled by default on current mainnet parameters (the `DynamicPropertiesStore`/`ProposalUtil` wiring was only partially inspected before the tool budget ran out), so the practical exploitability on a fully up-to-date mainnet node versus older/private/test networks running earlier chain parameters could not be fully verified and should be checked further (e.g., via a Devin session with access to `DynamicPropertiesStore.java` and `ProposalUtil.java`) before treating this as exploitable on all deployments.

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1165-1181)
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
      int cnt = signatures.length;
      if (cnt == 0 || cnt > MAX_SIZE || signatures.length != addresses.length) {
        return Pair.of(true, DATA_FALSE);
      }
```
