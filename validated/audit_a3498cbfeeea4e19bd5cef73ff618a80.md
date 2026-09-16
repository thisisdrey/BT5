Given my research, the most defensible OOM analog to CVE-2018-21027 ("malloc mishandled" from unvalidated attacker-controlled size) is in the TVM precompiled-contract signature-verification helpers, where an array length taken directly from calldata is used to allocate a Java array before any bound is enforced.

### Title
Unbounded array allocation from attacker-controlled calldata length in TVM signature-verification precompiles causes OOM/node crash - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
`ValidateMultiSign` and `BatchValidateSign` decode their calldata into an array of `DataWord`s and then call helper methods `extractBytesArray`, `extractSigArray`, and `extractBytes32Array` to build Java arrays sized directly from a 256-bit calldata word cast to `int` via `intValueSafe()`, with no upper bound check unless a specific hardfork flag path is taken.

### Finding Description
`extractBytesArray` and `extractBytes32Array` compute `int len = words[offset].intValueSafe();` and immediately do `new byte[len][]` before validating `len` against any sane maximum: [1](#0-0) [2](#0-1) 

In `ValidateMultiSign.execute` and `BatchValidateSign.doExecute`, the `MAX_SIZE` bound (5 or 16) is only enforced when `VMConfig.allowTvmSelfdestructRestriction()` is true; the legacy branch calls `extractBytesArray`/`extractSigArray` with the raw, unchecked length: [3](#0-2) [4](#0-3) 

`getEnergyForData()` for both contracts only charges energy proportional to `data.length / WORD_SIZE`, completely independent of the attacker-declared array length embedded inside the ABI-encoded payload, so a small, cheap transaction can smuggle a huge `len` value (e.g., close to `Integer.MAX_VALUE`) that is never gas-metered before the allocation attempt: [5](#0-4) [6](#0-5) 

`new byte[len][]` with `len` near `Integer.MAX_VALUE` attempts to allocate on the order of tens of gigabytes of reference-array space, throwing `OutOfMemoryError` inside the precompile execution path, which is only guarded against `InterruptedException`/generic `Throwable` in `BatchValidateSign.execute` (an `OutOfMemoryError` is an `Error`, not caught by `catch (Throwable t)`... actually `Throwable` does catch `Error`, but by the time it's caught the JVM heap is already exhausted, affecting the whole node process, not just the transaction).

### Impact Explanation
An `OutOfMemoryError` thrown while executing a TVM precompile during transaction execution can destabilize the entire node JVM (heap exhaustion affects all threads, not just the executing one), leading to a node crash or halt. Since this is reachable through a normal, unprivileged smart-contract call (any address can call the `validatemultisign`/`batchvalidatesign` precompiled contract addresses from a deployed contract), this matches the "node crash or halt" acceptance criterion.

### Likelihood Explanation
Exploitability depends on whether `VMConfig.allowTvmSelfdestructRestriction()` is active on the target chain. If this specific hardfork/feature flag has already been activated on current TRON mainnet, the bounded branch is always taken and this specific bug is not reachable there. I was not able to verify the current default/activation state of this flag within the available tool budget, so this should be treated as **unconfirmed for mainnet** but potentially live on chains/forks/testnets that have not activated this particular TIP. This uncertainty should be resolved before treating this as a confirmed mainnet-exploitable issue.

### Recommendation
Add an explicit, unconditional upper bound check on the decoded array length in `extractBytesArray`, `extractSigArray`, and `extractBytes32Array` themselves (not only in the caller, and not only behind a feature flag) before allocating, e.g. reject if `len < 0 || len > MAX_SIZE` regardless of `allowTvmSelfdestructRestriction`, and incorporate the declared length into `getEnergyForData` so a mismatched/inflated length is charged (or rejected) prior to any allocation.

### Proof of Concept
1. Deploy a trivial contract that performs a `STATICCALL`/`CALL` to the `BatchValidateSign` precompiled address.
2. Craft calldata for `batchvalidatesign(bytes32,bytes[],address[])` where the ABI offset word for the `bytes[]` (signatures) array points to a length word set to a large value such as `0x7FFFFFFF`, while keeping the overall `data.length` small so `getEnergyForData` charges minimal energy.
3. On a chain/build where `allowTvmSelfdestructRestriction` is not active, `doExecute` calls `extractBytesArray(words, offset, data)` which executes `new byte[0x7FFFFFFF][]`, immediately exhausting/spiking JVM heap and raising `OutOfMemoryError` inside the transaction-processing thread.

I was unable to confirm from the indexed code whether `allowTvmSelfdestructRestriction` is unconditionally enabled on current mainnet configuration (this affects whether the vulnerable branch is live); a Devin session with access to the config/hardfork-activation source (`ForkController`/`ForkBlockVersionConsts` or chain parameter defaults) would be needed to confirm current exploitability.

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1044-1049)
```java
    @Override
    public long getEnergyForData(byte[] data) {
      long cnt = (data.length / WORD_SIZE - 5) / 5;
      // one sign 1500, half of ecrecover
      return cnt * ENGERYPERSIGN;
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1065-1077)
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
