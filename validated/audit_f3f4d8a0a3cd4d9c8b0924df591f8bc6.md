### Title
Unbounded array allocation from attacker-controlled length in `PrecompiledContracts.extractBytesArray`/`extractBytes32Array` can OOM-crash the node - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
`CVE-2016-4809` is a class of bug where a length field taken directly from untrusted input is used to size a buffer/array without validating it against the actual data available, causing the parser to crash (OOM/DoS). The analogous pattern exists in java-tron's TVM precompiled-contract argument decoding, where a 32-byte word from calldata is converted to an `int` and used directly as an array dimension before any bound is enforced.

### Finding Description
`extractBytes32Array` and `extractBytesArray` read a length directly out of ABI-encoded calldata and immediately allocate an array of that size: [1](#0-0) 

`len` comes from `words[offset].intValueSafe()`, which merely clamps to `Integer.MAX_VALUE` on overflow rather than validating against the actual size of `data`/`words` [2](#0-1) . A transaction can therefore supply a word such as `0x7fffffff` and force `new byte[len][]` to allocate up to ~2^31 array slots.

This is reached from `ValidateMultiSign.execute`, where the un-restricted code path calls `extractBytesArray` with no upper bound check at all, unlike the `allowTvmSelfdestructRestriction()`-gated branch, which validates `sigArraySize > MAX_SIZE` before calling `extractSigArray`: [3](#0-2) 

If `VMConfig.allowTvmSelfdestructRestriction()` is not yet activated on a given network (it is a committee-proposal-gated hard fork flag, per `chainbase/src/main/java/org/tron/core/store/DynamicPropertiesStore.java` and `actuator/src/main/java/org/tron/core/utils/ProposalUtil.java`), the `extractBytesArray` branch is taken unconditionally, with no size cap, on every call to the `ValidateMultiSign` precompile (address `0x0...151`, reachable via any contract's low-level `staticcall`/`call`).

### Impact Explanation
An attacker who deploys a contract that calls the `ValidateMultiSign` precompile with a crafted `len` word can force the node's JVM to attempt an allocation of an oversized `byte[][]` (or nested `byte[]` arrays), triggering an `OutOfMemoryError`. Depending on how/where the precompile invocation is wrapped, this can crash the full node process or a validating/witness node processing the block, producing node crash/denial-of-service — one of the accepted impact categories for this analog (node crash or halt of an API/execution path).

### Likelihood Explanation
The path is reachable by any unprivileged account submitting a single `TriggerSmartContract` transaction that calls a deployed contract performing the precompile call — no special privilege, witness role, or peer access is required, matching the reachability bar (single signed transaction/contract call). The only mitigating factor is that on networks where `allowTvmSelfdestructRestriction` has already been activated via governance proposal, the vulnerable unbounded branch is dead code for this specific call site; I was not able to confirm from the indexed code whether this flag is enabled by default at genesis or only via proposal (it is proposal-gated per `DynamicPropertiesStore`/`ProposalUtil` matches found), so likelihood is network/config dependent.

### Recommendation
Enforce an upper bound on `len` in `extractBytes32Array`/`extractBytesArray` before allocation (e.g., compare against `words.length - offset - 1` and/or a fixed `MAX_SIZE` constant, mirroring the check already applied in the `allowTvmSelfdestructRestriction` branch), independent of the hard-fork flag, so the un-gated call path in `ValidateMultiSign.execute` cannot allocate arbitrarily large arrays.

### Proof of Concept
1. Deploy a contract that performs a low-level call to the `ValidateMultiSign` precompiled contract address with calldata where the signature-array length word (at the offset pointed to by `words[3]`) is set to a large value such as `0x000000000000000000000000000000000000000000000000000000007FFFFFFF`.
2. On a network where `allowTvmSelfdestructRestriction()` is not active, `ValidateMultiSign.execute` calls `extractBytesArray(words, offset, rawData)` directly (`PrecompiledContracts.java:1074`), which executes `new byte[len][]` with `len` ≈ `Integer.MAX_VALUE`.
3. The JVM throws `OutOfMemoryError` during the allocation attempt, which — depending on the calling context's exception handling for `Throwable`/`Error` in TVM execution — can propagate and crash the node process handling the transaction.

*Note:* I could not fully verify within the tool budget (a) whether an outer `catch` for `OutOfMemoryError`/`Throwable` exists around precompile `execute()` invocation in `Program.java` that would downgrade this to a caught, non-fatal exception, or (b) the default activation state of `allowTvmSelfdestructRestriction` on mainnet. These would materially affect the actual severity and should be confirmed by a full code review/live-testing (e.g., in a background Devin session) before treating this as a confirmed unconditional crash.

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

**File:** common/src/main/java/org/tron/common/runtime/vm/DataWord.java (L219-229)
```java
  /**
   * In case of int overflow returns Integer.MAX_VALUE otherwise works as #intValue()
   */
  public int intValueSafe() {
    int bytesOccupied = bytesOccupied();
    int intValue = intValue();
    if (bytesOccupied > 4 || intValue < 0) {
      return Integer.MAX_VALUE;
    }
    return intValue;
  }
```
