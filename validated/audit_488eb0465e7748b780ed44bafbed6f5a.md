### Title
Unbounded array allocation from unchecked length fields in TVM `BatchValidateSign` precompile - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The `BatchValidateSign` precompiled contract (address `0x66`) parses attacker-controlled calldata into `DataWord[]` and then reads a "length" word directly out of that data to size two Java arrays (`byte[][]`) before any bound is enforced. This mirrors the CVE-2019-14524 pattern where a large, unchecked "count" field taken from untrusted input is used directly to size a buffer/array allocation.

### Finding Description
In `extractBytes32Array`, the length used to allocate the array is read straight from calldata with no upper-bound check at all: [1](#0-0) 

Compare this with the sibling helper `extractBytesArray`, which at least guards against an out-of-range `offset`, but still allocates `new byte[len][]` using an unchecked `len` taken from data: [2](#0-1) 

In `doExecute`, the upper-bound guard (`MAX_SIZE = 16`) that would prevent an oversized allocation is only applied when `VMConfig.allowTvmSelfdestructRestriction()` is enabled, and even then only checks `sigArraySize`/`addrArraySize` computed from a single word read from the array, not the actual value used inside `extractBytes32Array`/`extractBytesArray` themselves: [3](#0-2) 

`words[offset].intValueSafe()` returns an `int` derived directly from a 256-bit word supplied in the transaction's call data (`DataWord.parseArray(data)`), so an attacker can set this "count" field to a very large value (up to `Integer.MAX_VALUE`), causing the JVM to attempt to allocate a huge `byte[][]` before any real validation of that count against the actual size of `data` occurs — directly analogous to `fmt_mtm_load_song` using an unchecked "number of patterns" value to size a heap buffer.

### Impact Explanation
A successful trigger causes an `OutOfMemoryError` while executing a precompiled contract during transaction/contract execution. Although `execute()` wraps the call in `catch (Throwable t)`, throwing and catching `OutOfMemoryError` can leave the JVM heap in a degraded state, disrupt concurrent transaction processing across the node (since precompile execution runs inside the shared node process/heap), and depending on GC behavior can cause other threads (net, RPC, block application) to fail allocations, i.e., a node-level denial-of-service reachable from any TVM contract call.

### Likelihood Explanation
This is reachable by any unprivileged account by deploying or invoking a contract that calls the precompiled address for `BatchValidateSign`, supplying crafted calldata with an inflated word value at the addresses-array length offset. No special privileges, staking, or existing state are required — a bare `TriggerSmartContract` call suffices. The `getEnergyForData` cost model is based on the size of the supplied calldata, not on the value encoded inside it, so the attacker can pay minimal energy while causing a much larger allocation attempt inside `doExecute`.

### Recommendation
Enforce a strict `MAX_SIZE` bound on any length value read out of `words[...]` before it is used to size an array in `extractBytes32Array` and `extractBytesArray`/`extractSigArray`, regardless of the `allowTvmSelfdestructRestriction` feature flag, and validate the length against the remaining `words.length` (as already partially done in `extractBytesArray`) prior to allocation, not just for the top-level counts already checked in `doExecute`.

### Proof of Concept
1. Deploy any contract, or use an existing one, and construct calldata targeting the `BatchValidateSign` precompiled address with:
   - `words[0]` = arbitrary hash
   - `words[1]`/`words[2]` = offsets pointing to attacker-chosen positions
   - the word at the "addresses array length" position set to a very large value (e.g. `0x7FFFFFFF`)
2. Call the precompile via `TriggerSmartContract` (small calldata, so `getEnergyForData` charges minimal energy).
3. `extractBytes32Array`/`extractBytesArray` executes `new byte[len][]` with `len` derived from the attacker-controlled word, attempting a multi-gigabyte allocation and triggering `OutOfMemoryError` inside precompile execution on the full node.

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1156-1181)
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
      int cnt = signatures.length;
      if (cnt == 0 || cnt > MAX_SIZE || signatures.length != addresses.length) {
        return Pair.of(true, DATA_FALSE);
      }
```
