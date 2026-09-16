### Title
Unbounded array allocation from attacker-controlled length in `ValidateMultiSign`/`BatchValidateSign` precompiles causes node OOM/DoS - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
Both the `ValidateMultiSign` and `BatchValidateSign` TVM precompiled contracts extract array lengths directly from attacker-controlled calldata words and use them to allocate Java arrays before any bound is checked, mirroring the Dompdf bug class: "dimension validation happens early [only on a gated code path], but the resource allocation ... does not strictly limit ... memory usage for [an input] that has passed the initial check."

### Finding Description
`extractBytesArray` and `extractBytes32Array` read a length directly from an arbitrary `DataWord` in the ABI-encoded calldata and immediately allocate an array of that size, with no upper bound check inside the helper itself: [1](#0-0) 

In `ValidateMultiSign.execute`, the `MAX_SIZE` (5) bound check on the signature-array length is only performed when `VMConfig.allowTvmSelfdestructRestriction()` is enabled, and even then it gates the safer `extractSigArray` path; when the flag is disabled the legacy `extractBytesArray(words, words[3].intValueSafe() / WORD_SIZE, rawData)` is invoked directly with the attacker-supplied length and no prior bound check: [2](#0-1) 

`BatchValidateSign.doExecute` has the same pattern — the `sigArraySize`/`addrArraySize` guard only runs under the `allowTvmSelfdestructRestriction` flag, and `extractBytes32Array(words, words[2].intValueSafe() / WORD_SIZE)` for `addresses` is called with an unchecked length in all cases: [3](#0-2) 

The `getEnergyForData` cost for both contracts is computed purely from the physical `data.length` of the calldata (`(data.length / WORD_SIZE - 5) / 6`, etc.), not from the value encoded inside a length word: [4](#0-3) 

This means a caller can submit a tiny calldata buffer (a handful of 32-byte words, cheap in energy) while setting one of the length words to a huge value (e.g. `0x7FFFFFFF`). `intValueSafe()` on `DataWord` converts this to a Java `int` without capping it to any sane application-level bound, so `new byte[len][]` (or `new byte[len]`/`new byte[len*32]` downstream) attempts to allocate an array with ~2 billion reference slots — many gigabytes — well before the `signatures.length > MAX_SIZE` post-check ever executes. The allocation attempt itself either throws `OutOfMemoryError` (crashing/destabilizing the executing thread and potentially the whole JVM if headroom is exhausted) or forces a large GC pause, exactly analogous to Dompdf allocating an uncontrolled in-memory bitmap after only a superficial dimension check.

Both precompiles are reachable by any address via a plain `CALL`/`STATICCALL`/`DELEGATECALL` from a smart contract triggered by an unauthenticated/unprivileged `TriggerSmartContractTransaction`, through `Program.callToPrecompiledAddress`: [5](#0-4) 

The mitigation (`allowTvmSelfdestructRestriction`) is a chain-parameter-gated feature switch, not a hard fix in the helper functions themselves; the underlying `extractBytesArray`/`extractBytes32Array` remain exploitable whenever this flag is off, and there is no code path that unconditionally bounds these lengths before allocation.

### Impact Explanation
A single crafted transaction invoking `validatemultisign(...)` or `batchvalidatesign(...)` with a manipulated length word can force the executing full/witness node to attempt an allocation of multiple gigabytes, causing `OutOfMemoryError`, severe GC pauses, or process termination on the node executing/validating the block — a Denial of Service against block production/validation reachable from any unprivileged account, satisfying the "node crash or halt" bar for Medium/High severity.

### Likelihood Explanation
Likelihood is high for nodes where `allowTvmSelfdestructRestriction` has not been activated by committee proposal: any account can deploy a trivial contract that calls these fixed precompile addresses with malicious calldata, at very low TRX/energy cost, with no special privileges, and can be replayed by any node re-executing the transaction (both proposing and syncing nodes), making this a broadly reachable, deterministic DoS.

### Recommendation
Move the `MAX_SIZE`/length bound check unconditionally to the top of `extractBytesArray` and `extractBytes32Array` themselves (or immediately before every call site), independent of `allowTvmSelfdestructRestriction`, so that any decoded array length is validated against `MAX_SIZE` before any allocation occurs; reject with `Pair.of(true, DATA_FALSE)` (or equivalent) as soon as the length is out of range, rather than after the array has already been allocated and populated.

### Proof of Concept
1. Deploy a trivial contract that performs a low-level `call` to the `BatchValidateSign` precompile address (`0x66...` per `PrecompiledContracts` address table) with ABI-encoded input for `batchvalidatesign(bytes32,bytes[],address[])` where the `hash` word and outer array offsets are minimal/valid but the length word at the `addresses` array offset is set to `0x7FFFFFFF` instead of a real element count.
2. Send this call on a network where `allowTvmSelfdestructRestriction` has not been activated (default/older chain state), via a normal `TriggerSmartContract` transaction from any unprivileged account.
3. `words[2].intValueSafe()` yields `0x7FFFFFFF`; `extractBytes32Array` executes `new byte[0x7FFFFFFF][]`, attempting to allocate on the order of tens of gigabytes of reference storage, triggering `OutOfMemoryError` in the executing node's JVM (or a multi-second GC stall) for what the energy model priced as a near-zero-length input.
4. Repeating the transaction from multiple accounts amplifies the effect across all nodes re-executing the block, producing a sustained node-level Denial of Service.

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

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L1734-1752)
```java
    long requiredEnergy = contract.getEnergyForData(data);
    if (requiredEnergy > msg.getEnergy().longValue()) {
      // Not need to throw an exception, method caller needn't know that
      // regard as consumed the energy
      this.refundEnergy(0, CALL_PRE_COMPILED); //matches cpp logic
      this.stackPushZero();
    } else {
      // Delegate or not. if is delegated, we will use msg sender, otherwise use contract address
      if (msg.getOpCode() == Op.DELEGATECALL) {
        contract.setCallerAddress(getCallerAddress().toTronAddress());
      } else {
        contract.setCallerAddress(getContextAddress());
      }
      // this is the depositImpl, not contractState as above
      contract.setRepository(deposit);
      contract.setResult(this.result);
      contract.setConstantCall(isConstantCall());
      contract.setVmShouldEndInUs(getVmShouldEndInUs());
      Pair<Boolean, byte[]> out = contract.execute(data);
```
