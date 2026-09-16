Based on my research, the closest reachable analog to CVE-2016-9317 (oversized-size allocation causing a DoS hang) in java-tron is in the TVM precompiled-contract input decoders, where an attacker-controlled length word is used directly to allocate a Java array with no bound against the actual available input data.

### Title
Unbounded array allocation from attacker-controlled length in TVM precompiled-contract ABI decoding causes node OOM/DoS - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
`PrecompiledContracts.extractBytes32Array` and `extractBytesArray` read a 32-byte "length" word directly from the precompiled-contract call `data` supplied by any transaction/contract caller, and use it verbatim as the size of a newly-allocated Java array (`new byte[len][]`), without ever checking it against the actual remaining size of `data`.

### Finding Description
`extractBytes32Array` does: [1](#0-0) 
and `extractBytesArray` does: [2](#0-1) 

In both cases `len` comes from `words[offset].intValueSafe()`, which is a raw 32-byte word taken from the calldata of a precompiled-contract call — fully controlled by whoever crafts the transaction/internal call that invokes the precompile. Unlike `extractBytesArray`'s outer bound check (`offset > words.length - 1`), there is no upper bound on `len` itself relative to the actual size of `data`/`words`. A caller can set this word to a very large value (up to `Integer.MAX_VALUE`), causing the JVM to attempt to allocate a huge `byte[][]` (or in `extractBytesArray`'s inner loop, subsequently call `extractBytes`, which does `Arrays.copyOfRange`), which throws `OutOfMemoryError` or otherwise stalls the JVM while zeroing/allocating the array. This mirrors the libgd bug class: a size value taken from untrusted input is used for allocation before validating it is sane relative to the actual data present, leading to a resource-exhaustion hang/crash.

This differs from the VM's general memory-extension path (`Memory.extend`, `EnergyCost.calcMemEnergy`), which is protected by the `MEM_LIMIT` (3 MB) check before any large allocation: [3](#0-2) 
The precompiled-contract argument decoders bypass that energy-gated allocation limit entirely — they operate on raw calldata words before/without going through `calcMemEnergy`/`checkMemorySize`.

### Impact Explanation
A successful trigger causes an `OutOfMemoryError` inside the node process handling the transaction/contract execution, which can crash or hang the full node (denial of service), matching the "node crash or halt" acceptance criterion. Because array allocation of this size is not gated by TVM energy accounting the way memory expansion is, the caller pays only base call energy while forcing large-scale JVM heap allocation attempts.

### Likelihood Explanation
Reachable by any address that can trigger a call into the affected precompiled contract with crafted `data` (e.g., via a deployed contract issuing a `STATICCALL`/`CALL` to the precompile address, or directly if the precompile is invoked from a transaction). No special privilege, signature validity, or SR/witness role is required — only crafting the length word in the call data.

### Recommendation
Add bound checks in `extractBytes32Array` and `extractBytesArray` (and the similarly patterned `extractSigArray`) so that `len` is validated against the remaining length of `words`/`data` before allocation, e.g. reject or clamp `len` if `len` exceeds `(data.length - offset*32)/32` or a sane maximum array-count constant, mirroring the `MEM_LIMIT` style guard already used in `EnergyCost`.

### Proof of Concept
Craft a transaction that calls the precompiled contract whose input decoding invokes `extractBytesArray`/`extractBytes32Array`, with the length word (`words[offset]`) set to a very large value (e.g., `0x7FFFFFFF`). The decoder immediately attempts `new byte[][] bytesArray = new byte[len][]`/`new byte[len]`, causing excessive heap allocation attempts and an `OutOfMemoryError`/hang in the executing node, before any subsequent bounds validation against actual data content is reached.

**Note on confidence**: I was not able to fully trace, within the available tool budget, which specific precompiled contract(s) call `extractBytes32Array`/`extractBytesArray`/`extractSigArray` at the call-site level (e.g. confirming the exact precompile address and energy cost function gating it), so the exact end-to-end reachability from a transaction should be verified against `PrecompiledContracts.getContractForAddress` and each precompile's `execute`/`getEnergyForData` implementation before treating this as fully confirmed.

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

**File:** actuator/src/main/java/org/tron/core/vm/EnergyCost.java (L549-576)
```java
  private static long calcMemEnergy(long oldMemSize, BigInteger newMemSize,
                             long copySize, int op) {
    long energyCost = 0;

    checkMemorySize(op, newMemSize);

    // memory SUN consume calc
    long memoryUsage = (newMemSize.longValueExact() + 31) / 32 * 32;
    if (memoryUsage > oldMemSize) {
      long memWords = (memoryUsage / 32);
      long memWordsOld = (oldMemSize / 32);
      long memEnergy = (MEMORY * memWords + memWords * memWords / 512)
          - (MEMORY * memWordsOld + memWordsOld * memWordsOld / 512);
      energyCost += memEnergy;
    }

    if (copySize > 0) {
      long copyEnergy = COPY_ENERGY * ((copySize + 31) / 32);
      energyCost += copyEnergy;
    }
    return energyCost;
  }

  private static void checkMemorySize(int op, BigInteger newMemSize) {
    if (newMemSize.compareTo(MEM_LIMIT) > 0) {
      throw Program.Exception.memoryOverflow(op);
    }
  }
```
