### Title
Uncontrolled Resource Consumption via Unbounded Array Allocation in `BatchValidateSign` Precompile - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
The `BatchValidateSign` TVM precompiled contract calls `extractBytes32Array()` to parse the "addresses" array from attacker-controlled calldata without first validating the declared array length against any upper bound, unlike the sibling signature-array path which is guarded by a `MAX_SIZE` check under one feature-flag branch. An attacker can craft calldata that encodes an enormous array length, causing the node to attempt allocating a huge `byte[][]` on every execution/re-execution of the transaction, leading to `OutOfMemoryError` / excessive GC pressure and node crash or halt.

### Finding Description
`extractBytes32Array` reads the length word directly from the caller-supplied `data` and allocates an array of that size before any bound check occurs: [1](#0-0) 

In `BatchValidateSign.doExecute`, the bound check on the addresses-array length (`addrArraySize > MAX_SIZE`) is only performed when `VMConfig.allowTvmSelfdestructRestriction()` returns true. `extractBytes32Array` itself, however, is invoked unconditionally on every call regardless of that flag: [2](#0-1) 

Because `words[offset].intValueSafe()` can be driven by attacker-controlled calldata up to a large 32-bit value, `new byte[len][]` at line 392 can request an allocation of billions of array-slot references (~8 bytes each on 64-bit JVMs, i.e. tens of gigabytes), long before the later `cnt > MAX_SIZE` sanity check at line 1179 is ever reached — the OOM/latency hit occurs during allocation itself. This mirrors the reported Bouncy Castle CVE-2025-12194 pattern: sizes taken from untrusted input are used to drive allocation ("Excessive Allocation") without a pre-allocation bound check.

`extractBytesArray` (used for the signature side when the feature flag is disabled) has the same unchecked-length allocation pattern: [3](#0-2) 

### Impact Explanation
Any account can invoke a smart contract that calls the `BatchValidateSign` precompiled contract address with crafted calldata. If the runtime's feature flag `allowTvmSelfdestructRestriction` is not active, the node will attempt to allocate an oversized array purely from calldata content, causing memory exhaustion. On a shared full/witness node this can trigger `OutOfMemoryError`, degrade or crash the JVM, or force GC thrashing across the network as every node re-executes the same broadcast transaction — a chain-wide denial-of-service condition without requiring any signature validity or special privileges from the caller.

### Likelihood Explanation
Reachability requires only a normal contract call (`TriggerSmartContract`) targeting the precompile address that maps to `BatchValidateSign`, with calldata under the caller's control — no elevated permission or SR/witness role is needed. The energy metering for this precompile (`getEnergyForData`) is computed from `data.length`, not from the value encoded inside the data, so a small calldata payload encoding a huge length word can bypass energy-based cost limiting for the resulting allocation attempt.

### Recommendation
Move the `addrArraySize`/`sigArraySize` (and any array-length word used in `extractBytesArray`/`extractSigArray`/`extractBytes32Array`) bound checks so they execute unconditionally, before any array is allocated, regardless of `VMConfig.allowTvmSelfdestructRestriction()`. Enforce `MAX_SIZE` (or a similarly small sane limit) inside `extractBytes32Array`/`extractBytesArray`/`extractSigArray` themselves so the guard cannot be bypassed by future flag combinations, and reject with `Pair.of(false, EMPTY_BYTE_ARRAY)` on out-of-range lengths before allocating.

### Proof of Concept
1. Deploy or call an existing contract that performs a `STATICCALL`/`CALL` to the `BatchValidateSign` precompile address.
2. Craft the calldata so that `words[2]` (the addresses-array offset word) points to a word whose value (interpreted via `intValueSafe()`) is a very large integer (e.g., close to `Integer.MAX_VALUE / 32`).
3. Ensure the environment has `allowTvmSelfdestructRestriction` disabled (its default/historical state before the corresponding hard fork activates), so the `addrArraySize > MAX_SIZE` guard at line 1168 is skipped.
4. Broadcast the transaction; when `BatchValidateSign.doExecute` reaches `extractBytes32Array(words, offset)` at line 1176-1177, the node attempts `new byte[len][]` with the attacker-supplied huge `len`, exhausting heap memory and crashing or stalling the executing node.

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
