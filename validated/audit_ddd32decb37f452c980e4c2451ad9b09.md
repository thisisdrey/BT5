Based on my investigation, I found a strong analog: unbounded array allocation in the `BatchValidateSign`/`ValidateMultiSign` precompiled contract helpers in `PrecompiledContracts.java`, driven entirely by attacker-supplied calldata, mirroring the Ghidra OOM bug class (unbounded output-buffer allocation from untrusted, attacker-controlled length fields).

### Title
Out-of-Memory via unbounded array allocation in TVM precompiled contract input parsing - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
`extractBytes32Array`, `extractBytesArray`, and `extractSigArray` in `PrecompiledContracts.java` read an attacker-controlled length word directly from calldata and immediately allocate a Java array of that size, with no upper bound check before allocation, analogous to the unbounded buffer allocation in Ghidra's `rust_demangle`.

### Finding Description
`extractBytes32Array` reads `len` from `words[offset].intValueSafe()` and allocates `new byte[len][]` with no maximum size validation: [1](#0-0) 

Similarly, `extractBytesArray` and `extractSigArray` derive `len` the same way from raw `words` (decoded directly from the precompile call's input `data`) and allocate arrays sized by that value before any range/sanity check is applied: [2](#0-1) 

`words[offset].intValueSafe()` can return any value up to `Integer.MAX_VALUE` since it comes straight from user-supplied ABI-encoded calldata to a precompiled contract (e.g. `batchValidateSign`/`validateMultiSign`, reachable via `getContractForAddress` when `VMConfig.allowTvmSolidity059()` is enabled). There is a `isValidAbiEncoding` helper in the same file, but it is not shown to gate these specific extraction helpers before the allocation occurs: [3](#0-2) 

The precompile dispatch itself confirms these are reachable from ordinary contract calls once the relevant hard fork flags are enabled: [4](#0-3) 

### Impact Explanation
If `len` is attacker-controlled and unbounded (e.g., a crafted 32-byte word near `0x7FFFFFFF`), `new byte[len][]` or the nested per-element `byte[192]`/`byte[64]` allocations (as also seen in the Sapling proof verification arrays) can trigger massive heap allocation attempts, causing an `OutOfMemoryError` in the node process executing the transaction. Because TVM execution happens inside the full node process (not a sandboxed subprocess), an uncontrolled `OutOfMemoryError` here risks crashing or destabilizing the validating/witness node — a node crash/halt impact matching the "Medium" severity class of the reference CVE.

### Likelihood Explanation
Reachability requires only a single crafted transaction calling into the `batchValidateSign`/`validateMultiSign`/Sapling-related precompiled contracts (all reachable via ordinary `TriggerSmartContract` transactions once the associated `allowTvmSolidity059`/`allowShieldedTRC20Transaction` chain parameters are active), making this exploitable by any unprivileged contract caller. However, I could not fully verify within the available index whether an energy-cost/gas charge computed prior to calling these extraction helpers (e.g., `getEnergyForData`) already effectively bounds `len` before the array allocation is reached — this needs to be confirmed by reading the full `execute`/`getEnergyForData` implementations for `BatchValidateSignContract`/`ValidateMultiSignContract` in `PrecompiledContracts.java`, which extend beyond what was retrieved.

### Recommendation
Add an explicit maximum-length check (bounded by `data.length / WORD_SIZE` or a hard cap) immediately after reading `len` in `extractBytes32Array`, `extractBytesArray`, and `extractSigArray`, before allocating the output arrays, and ensure `getEnergyForData` charges energy proportional to the declared array length so a malicious/underfunded caller cannot force large allocations for free.

### Proof of Concept
Craft a `TriggerSmartContract` transaction whose contract internally issues a `STATICCALL`/`CALL` to the `batchValidateSign` precompile address (`0000...1000006`) with calldata whose length-array word (the `len` field consumed by `extractBytes32Array`/`extractBytesArray`) is set to a very large value (e.g. `0x7FFFFFFF`) while the actual `data` array is small; this causes `new byte[len][]` to be allocated with an oversized length, which is unverified before allocation.

**Note on confidence**: I was unable to fully trace the exact `execute()` bodies of `batchValidateSign`/`validateMultiSign` or their `getEnergyForData` cost functions to confirm whether energy metering already caps `len` before these helpers run — this is an important gap that a background Devin session with full file access could close by reading the complete `PrecompiledContracts.java` (it is a very large file and portions were not retrievable from the index).

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L254-259)
```java
    if (VMConfig.allowTvmSolidity059() && address.equals(batchValidateSignAddr)) {
      return batchValidateSign;
    }
    if (VMConfig.allowTvmSolidity059() && address.equals(validateMultiSignAddr)) {
      return validateMultiSign;
    }
```

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L399-426)
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

  private static byte[][] extractSigArray(DataWord[] words, int offset, byte[] data) {
    if (offset > words.length - 1) {
      return new byte[0][];
    }
    int len = words[offset].intValueSafe();
    byte[][] bytesArray = new byte[len][];
    for (int i = 0; i < len; i++) {
      int bytesOffset = words[offset + i + 1].intValueSafe() / WORD_SIZE;
      bytesArray[i] = extractBytes(data, (bytesOffset + offset + 2) * WORD_SIZE,
          SIG_LENGTH);
    }
    return bytesArray;
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L432-438)
```java
  private static boolean isValidAbiEncoding(byte[] data, int headerWords, int itemWords) {
    if (data == null || data.length % WORD_SIZE != 0) {
      return false;
    }
    long tail = subtractExact(data.length, multiplyExact(headerWords, WORD_SIZE));
    return tail > 0 && tail % multiplyExact(itemWords, WORD_SIZE) == 0;
  }
```
