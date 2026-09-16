### Title
Unvalidated array-length parameter in `ValidateMultiSign`/`BatchValidateSign` precompiles causes unguarded `byte[len][]` allocation - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The `ValidateMultiSign` and `BatchValidateSign` TVM precompiled contracts read an array-length word directly out of attacker-supplied calldata and use it, unchecked, as the size of a newly allocated Java array (`new byte[len][]`) in `extractBytesArray`/`extractSigArray`/`extractBytes32Array`. This mirrors the CVE-2025-39973 bug class: a caller-controlled length field is assigned into a resource-allocation path without an upper-bound or non-negativity check before it is committed. In java-tron the size check (`sigArraySize > MAX_SIZE`) is only performed when the `allowTvmSelfdestructRestriction` hard-fork flag is active; the legacy code path that runs when that flag is off calls the unchecked extraction helpers directly.

### Finding Description
`extractBytesArray`, `extractSigArray`, and `extractBytes32Array` take the element count directly from `words[offset].intValueSafe()` — a 256-bit word fully controlled by the contract call's `data` payload — and immediately do: [1](#0-0) 

No check is made that `len` is non-negative or bounded before `new byte[len][]` / `new byte[len][]` is executed, and no check is made that `offset + i + 1` stays within the `words` array bounds before indexing into it.

In `ValidateMultiSign.execute`, the size guard is conditional on a config flag: [2](#0-1) 

When `VMConfig.allowTvmSelfdestructRestriction()` is `false`, `extractBytesArray` is invoked with no prior bound check on `sigArraySize`/`len`, so a caller can supply an arbitrarily large or negative length word. The equivalent unguarded call exists in `BatchValidateSign.doExecute`: [3](#0-2) 

Unlike the `Sapling`-style precompile (which validates `spendCount`/`receiveCount` against a hard `[1,2]` bound before any allocation, see `PrecompiledContracts.java:1492-1499`), these signature-validation precompiles rely entirely on a later hard-fork flag to add the bound — exactly the "validation added after the fact" pattern described in the kernel `ring_len` fix.

Note: `execute()` for `ValidateMultiSign` only wraps the *signature-recovery/permission* logic in `try { ... } catch (Throwable t)`; the array-extraction calls at lines 1057-1077 sit **outside** that try block, so a `NegativeArraySizeException` (negative `len`) or an `OutOfMemoryError` (huge `len`, which is an `Error`, not caught by generic `catch (Exception)` handlers elsewhere in the interpreter) thrown from `extractBytesArray`/`extractSigArray` propagates unguarded out of the precompile call.

### Impact Explanation
Any account can invoke `validatemultisign(...)` or `batchvalidatesign(...)` on address `0x...a` / `0x...9` from a plain transaction or contract call, supplying crafted calldata where the length word for the signature/address array is negative or extremely large. This can:
- Throw an uncaught `NegativeArraySizeException`/`OutOfMemoryError` during TVM execution of a precompile, which is a query/broadcast path reachable by any unprivileged transaction sender.
- Potentially crash or destabilize the node process (OOM) or unexpected propagation of an `Error` through the interpreter loop, matching the "node crash or halt" acceptance criterion.

This does not directly enable fund theft, but is a denial-of-service class issue directly analogous to the referenced kernel bug (unchecked length assigned into a resource-allocation/HMC context without bound validation).

### Likelihood Explanation
Reachability depends on the runtime value of `VMConfig.allowTvmSelfdestructRestriction()`. This is a chain-parameter/hard-fork gate — I was not able to fully confirm from the available index whether it is unconditionally enabled on current mainnet or still toggle-controlled per `DynamicPropertiesStore`/`ForkController`. If the guard is active network-wide, the vulnerable legacy branch is currently unreachable in production, which significantly reduces likelihood. If any network/deployment (or a future rollback/branch) runs with the flag disabled, exploitation requires only a single crafted transaction with no special privileges.

### Recommendation
Move the `sigArraySize`/`addrArraySize` (and general array-length) validation in `extractBytesArray`, `extractSigArray`, and `extractBytes32Array` out of the `allowTvmSelfdestructRestriction()`-gated branch and make it unconditional: reject (return `false`) whenever the parsed length is negative or exceeds `MAX_SIZE`/a sane hard cap, and validate `offset + i + 1 < words.length` before any indexing, mirroring the unconditional `spendCount`/`receiveCount` bound check already present in the Sapling precompile.

### Proof of Concept
Not able to fully construct within this environment (no code execution / bytecode assembly tooling available here); conceptually: craft calldata for `validatemultisign(bytes32,uint256,bytes,bytes[])` or `batchvalidatesign(bytes32,bytes[],address[])` where the ABI-encoded dynamic array's length word (e.g., the word at the offset pointed to by `words[3]`/`words[1]`) is set to `0xFFFFFFFF` or a two's-complement negative value, and submit it via a normal TVM contract call with `allowTvmSelfdestructRestriction` disabled — `extractBytesArray`/`extractSigArray` will attempt `new byte[len][]` with the attacker-chosen size before any bound check runs.

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
