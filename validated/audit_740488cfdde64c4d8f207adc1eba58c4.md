## Title
Unauthenticated Denial-of-Service via Unbounded Array Allocation in `ValidateMultiSign`/`BatchValidateSign` Precompiles - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The `ValidateMultiSign` and `BatchValidateSign` TVM precompiled contracts decode ABI-encoded dynamic arrays from calldata using helper methods `extractBytes32Array`, `extractBytesArray`, and `extractSigArray`. These helpers read an attacker-controlled 32-byte length field from the calldata and immediately allocate a Java array sized to that value (`new byte[len][]`) with no upper bound check on `len` prior to allocation, mirroring the mdex `parse_highlight_lines` bug class: a user-controlled range/count value is eagerly expanded into a heap allocation before any bound validation.

### Finding Description
`extractBytes32Array`, `extractBytesArray`, and `extractSigArray` read `int len = words[offset].intValueSafe();` and then execute `byte[][] bytesArray = new byte[len][];` before any range check is performed: [1](#0-0) 

These are invoked from the `ValidateMultiSign` precompile: [2](#0-1) 

and from `BatchValidateSign`: [3](#0-2) 

Both call sites gate the `MAX_SIZE` bound check on the feature flag `VMConfig.allowTvmSelfdestructRestriction()`. When that flag is enabled, `sigArraySize`/`addrArraySize` are read and compared against `MAX_SIZE` (5 or 16) *before* calling `extractSigArray`, preventing the issue in that code path. However, when the flag is **disabled**, the code falls straight into `extractBytesArray(...)` / (implicitly) `extractBytes32Array(...)` with no pre-check at all — the `MAX_SIZE` bound is only enforced *after* the array has already been allocated (`if (signatures.length == 0 || signatures.length > MAX_SIZE)`), which is too late since the OOM/allocation already happened inside the helper.

The energy cost for these precompiles, computed via `getEnergyForData`, is based on `data.length` (the actual size of the calldata blob supplied), not on the value encoded in the length word: [4](#0-3) [5](#0-4) 

This means an attacker can submit a very small calldata blob (cheap in energy) that nonetheless encodes a length field (`words[offset]`) up to `Integer.MAX_VALUE`, forcing `new byte[Integer.MAX_VALUE][]` (or similarly for `extractBytes32Array`) to be attempted, causing an `OutOfMemoryError` in the node process that is executing the transaction — independent of the fee actually paid.

### Impact Explanation
An `OutOfMemoryError` thrown during transaction execution inside the TVM can propagate and destabilize/crash the executing node process (or at minimum abort block processing for that node), producing a denial-of-service condition reachable by any account able to broadcast a `TriggerSmartContract` transaction that calls a contract invoking the `validatemultisign`/`batchvalidatesign` precompile addresses. Because energy metering does not scale with the attacker-declared array length (only with actual calldata size), the attack is cheap relative to its potential memory-allocation cost, satisfying the "no impact analog rejected" bar via node crash risk.

### Likelihood Explanation
Reaching these precompiles only requires deploying/calling a smart contract that performs a `STATICCALL`/`CALL` to the `ValidateMultiSign` or `BatchValidateSign` precompile addresses with attacker-crafted calldata — an operation available to any unprivileged contract deployer/caller. The vulnerable path is gated behind `VMConfig.allowTvmSelfdestructRestriction()` being disabled; whether this flag is enabled by default on mainnet needs to be confirmed against current chain parameters, which limits certainty about current exploitability on production networks, but the vulnerable code path exists unconditionally in the source.

### Recommendation
Add an explicit upper-bound check on `len` (and derived offsets) in `extractBytes32Array`, `extractBytesArray`, and `extractSigArray` before performing `new byte[len][]`, independent of the `allowTvmSelfdestructRestriction` flag, and validate that the declared length is consistent with the actual size of `words`/`data` before allocation (similar to the `isValidAbiEncoding` check already used elsewhere in the file).

### Proof of Concept
1. Deploy a contract that calls the `ValidateMultiSign` precompile (`0x0000...66` or equivalent address) via `STATICCALL`, passing ABI-encoded calldata where the signature-array-length word (`words[3]`) is set to a small offset pointing to a length word containing `0x7FFFFFFF` while the remainder of the calldata is minimal/short.
2. With `VMConfig.allowTvmSelfdestructRestriction()` disabled, `execute()` calls `extractBytesArray(words, offset, rawData)` directly.
3. `extractBytesArray` executes `byte[][] bytesArray = new byte[0x7FFFFFFF][];` before the `MAX_SIZE` check, attempting a multi-gigabyte allocation and triggering `OutOfMemoryError` while the transaction's declared energy usage (based on actual small `data.length`) remains low.

Note: I was unable to fully confirm from the indexed code whether `VMConfig.allowTvmSelfdestructRestriction()` defaults to enabled on current mainnet configuration (this flag typically becomes permanently `true` after a hard fork activation), which affects whether this path is presently reachable on production networks; a Devin session with full repo/config access would be needed to verify the current hard-fork/feature-flag activation status.

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1044-1049)
```java
    @Override
    public long getEnergyForData(byte[] data) {
      long cnt = (data.length / WORD_SIZE - 5) / 5;
      // one sign 1500, half of ecrecover
      return cnt * ENGERYPERSIGN;
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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1137-1142)
```java
    @Override
    public long getEnergyForData(byte[] data) {
      long cnt = (data.length / WORD_SIZE - 5) / 6;
      // one sign 1500, half of ecrecover
      return cnt * ENGERYPERSIGN;
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
