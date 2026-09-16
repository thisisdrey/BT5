### Title
Unbounded array allocation from attacker-controlled length field in `BatchValidateSign` precompile allows memory-exhaustion DoS with disproportionately low energy cost - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
The `BatchValidateSign` TVM precompiled contract decodes its calldata into 32-byte words and then treats one attacker-supplied word as an unchecked array-length used to allocate a `byte[][]` (via `extractBytesArray`/`extractBytes32Array`), before any bound is enforced against that length. Because the energy charged for the call (`getEnergyForData`) is computed from the raw calldata *length*, not from this internal length field, an attacker can submit a tiny, cheap payload that embeds a very large length value, forcing the node to allocate a huge array server-side for minimal energy cost.

### Finding Description
`BatchValidateSign.getEnergyForData` prices the call purely off `data.length`: [1](#0-0) 

`doExecute` then parses `data` into `DataWord[] words` and derives array sizes from words that are entirely attacker-controlled 32-byte values inside the calldata (not validated against the actual `data.length`): [2](#0-1) 

The `MAX_SIZE` guard on `sigArraySize`/`addrArraySize` only applies when `VMConfig.allowTvmSelfdestructRestriction()` is active, and even then it is applied to `extractSigArray`, not to the legacy `extractBytesArray` path taken otherwise: [3](#0-2) 

The underlying helpers allocate an array sized directly by a word taken from calldata before any relationship to the real payload size is checked: [4](#0-3) 

Crucially, `Program.callToPrecompiledAddress` only checks that `requiredEnergy <= msg.getEnergy()` (i.e., the cheap, calldata-length-based cost) before invoking `contract.execute(data)`, so the disproportionate internal allocation happens unconditionally once that cheap check passes: [5](#0-4) 

This mirrors the GitLab CVE-2020-13274 bug class: a small, cheaply-validated input (a "length" field) is trusted to size a large in-memory allocation without cross-checking it against the actual, bounded payload — enabling memory exhaustion far out of proportion to the cost paid by the requester.

### Impact Explanation
Any account can invoke this precompile via a normal `TriggerSmartContract` call (calling address `0x0...66`/whatever address `BatchValidateSign` is mapped to via `getContractForAddress`), paying only for `data.length`-based energy while forcing the node to allocate an oversized `byte[][]` (up to `Integer.MAX_VALUE` elements, each a reference — multiple gigabytes) inside `execute()`. Repeated/parallel invocations (further amplified by the shared `workers` executor thread pool used for non-constant calls) can drive heap pressure and GC pauses across the node, degrading or crashing block-producing/serving nodes — a resource/availability impact matching "node crash or halt" per the validation rules.

### Likelihood Explanation
High: the precompile is reachable by any unprivileged account issuing a signed `TriggerSmartContract` transaction with hand-crafted calldata; no special permissions, staking, or witness/SR status are required. Crafting the calldata only requires knowledge of the ABI-encoding layout consumed by `extractBytesArray`/`extractBytes32Array`.

### Recommendation
Before allocating `bytesArray`/`bytes32Array`, validate the extracted `len` against a strict upper bound (e.g., `MAX_SIZE`) and against the actual number of words available in `data`/`words` (i.e., `len <= words.length - offset - 1`), rejecting the call (returning `(false, EMPTY_BYTE_ARRAY)`) before allocation rather than after. Apply the same bound uniformly regardless of `VMConfig.allowTvmSelfdestructRestriction()` state, and consider tying `getEnergyForData` cost to any internally-declared array length as well as `data.length`.

### Proof of Concept
1. Compute the target precompile address for `BatchValidateSign` (as resolved by `PrecompiledContracts.getContractForAddress`).
2. Build calldata: `hash` (32 bytes) + `offset1` pointing to a location containing a length word set to a very large value (e.g., `0x7FFFFFFF`), plus a valid-looking `offset2` for the addresses array, keeping the overall `data.length` minimal so `getEnergyForData` returns a low cost (few thousand energy).
3. Send a `TriggerSmartContract` transaction calling this precompile with sufficient `feeLimit` to cover the low energy cost reported by `getEnergyForData`, but not proportional to the actual allocation size.
4. Observe that `doExecute` calls `extractBytesArray(words, offset, data)`, which executes `new byte[len][]` with `len` derived from the crafted word, causing a multi-gigabyte allocation attempt server-side for a transaction costing only the small `data.length`-based energy fee. [6](#0-5)

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1137-1142)
```java
    @Override
    public long getEnergyForData(byte[] data) {
      long cnt = (data.length / WORD_SIZE - 5) / 6;
      // one sign 1500, half of ecrecover
      return cnt * ENGERYPERSIGN;
    }
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
