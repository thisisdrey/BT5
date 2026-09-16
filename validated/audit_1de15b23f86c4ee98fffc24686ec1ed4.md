## Title
Unbounded array allocation from attacker-controlled length words in TVM `ValidateMultiSign`/`BatchValidateSign` precompiles causes node OOM/DoS - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The `ValidateMultiSign` (address `0x...0a`) and `BatchValidateSign` (address `0x...09`) TVM precompiled contracts decode ABI-style `bytes[]`/`address[]` array arguments by reading a "length" word directly out of the raw calldata and using it to size a Java array with `new byte[len][]`, without ever validating that `len` is bounded relative to the actual size of the supplied `data`. This mirrors the ExifReader `mluc` bug class (CWE-1284): the amount of memory allocated is driven by an attacker-controlled field rather than by the true size of the input, letting a caller with minimal calldata trigger allocation of gigabytes of heap.

### Finding Description
`extractBytesArray` and `extractBytes32Array` read the declared array length from a single 32-byte calldata word and immediately allocate an array of that size: [1](#0-0) 

`intValueSafe()` clamps any oversized/overflowing word to `Integer.MAX_VALUE` rather than rejecting it: [2](#0-1) 

So a crafted 32-byte word (e.g. `0x000000000000000000000000000000000000000000000000000000FFFFFFFF` or any value ≥ 2^32) decodes to `Integer.MAX_VALUE`, and `extractBytesArray`/`extractBytes32Array` execute `new byte[][](Integer.MAX_VALUE)` — an attempt to allocate an object-reference array sized in the gigabytes — before any check that this length is consistent with the actual length of `data`.

`ValidateMultiSign.execute()` calls `extractBytesArray` (or `extractSigArray`) directly using this attacker-supplied length, with a bounds check (`sigArraySize > MAX_SIZE`) that is only performed when `VMConfig.allowTvmSelfdestructRestriction()` is active; if that TIP is not activated, the unbounded allocation happens unconditionally: [3](#0-2) 

`BatchValidateSign.doExecute()` has an analogous guard for `signatures`, but `extractBytes32Array` (used to build `addresses`) is invoked unconditionally with no length check at all, regardless of whether the TIP flag is enabled: [4](#0-3) 

Critically, the energy charged for these precompiles (`getEnergyForData`) is computed purely from `data.length` — the physical size of the calldata — not from the "length" word embedded inside it: [5](#0-4) 

This decouples the energy cost from the actual memory-allocation cost of executing the contract, exactly analogous to ExifReader allocating memory proportional to an attacker-controlled tag field instead of the real ICC profile size — a small, cheap transaction can request an enormous allocation.

### Impact Explanation
Any account can deploy or call a smart contract that invokes address `0x...09` (`batchvalidatesign`) or `0x...0a` (`validatemultisign`) with crafted calldata containing an oversized length word in the `bytes[]`/`address[]` position. This triggers an attempted allocation of an array with up to `Integer.MAX_VALUE` elements inside the TVM execution of any full node processing the transaction/block. Such allocation attempts induce severe GC pressure or `OutOfMemoryError`; because `ValidateMultiSign.execute()` has no enclosing catch around this code path, an `OutOfMemoryError` here can propagate out of TVM execution during block processing, threatening node availability/crash (denial of service) across the network for a very low fee (energy charge is based only on the small physical calldata size).

### Likelihood Explanation
Reaching this code requires only a standard signed transaction that calls a contract (or is itself a `TriggerSmartContract`) invoking the `BatchValidateSign`/`ValidateMultiSign` precompiles, which are enabled via `VMConfig.allowTvmSolidity059()` — a widely activated TVM feature. No special privileges (SR, witness, peer) are needed; this is directly reachable by any unprivileged transaction broadcaster/contract caller, and the crafted calldata is trivial to construct (one oversized 32-byte word).

### Recommendation
- In `extractBytesArray`, `extractSigArray`, and `extractBytes32Array`, validate the decoded array length against the actual size of `words`/`data` (e.g., `len <= (data.length - offsetBytes) / WORD_SIZE`) before allocating, rejecting the call otherwise — independent of the `allowTvmSelfdestructRestriction` flag.
- Apply a hard upper bound (e.g., `MAX_SIZE`) check unconditionally in both `ValidateMultiSign` and `BatchValidateSign`, before calling any `extract*Array` helper, not only when the TIP-854 flag is active.
- Make `getEnergyForData` energy cost reflect the declared array-length word(s), not merely the physical calldata size, so any legitimate large allocation is properly metered.

### Proof of Concept
1. Construct calldata for `batchvalidatesign(bytes32,bytes[],address[])` (or `validatemultisign(...)`) where the ABI head correctly points to the `bytes[]` array offset, but the length word at that offset is set to `0xFFFFFFFF` (or any value causing `intValueSafe()` to clamp to `Integer.MAX_VALUE`), while the actual trailing calldata is only a few hundred bytes.
2. Submit a `TriggerSmartContract` transaction (with `VMConfig.allowTvmSelfdestructRestriction()` disabled, i.e. on a chain/testnet where TIP-854 has not activated) calling this precompile via a thin wrapper contract.
3. During execution, `extractBytesArray`/`extractBytes32Array` executes `new byte[Integer.MAX_VALUE][]`, causing the node's JVM to attempt a multi-gigabyte allocation, inducing severe GC pauses or `OutOfMemoryError` while only a small, cheap energy fee (based on actual calldata length) was charged.

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
