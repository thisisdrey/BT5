### Title
Uncontrolled Memory Allocation in `PrecompiledContracts.extractBytesArray` / `extractBytes32Array` via Crafted Precompile Calldata - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The `BatchValidateSign` precompiled contract (TVM address `0x9`) parses its ABI-encoded input with helper methods `extractBytes32Array` and `extractBytesArray`, which read an array-length word directly from attacker-controlled calldata and immediately use it to size a Java array allocation, without validating that the declared length is consistent with the actual size of the supplied `data` byte array. This mirrors the CImg `_load_analyze()` pattern: a header-derived size field is trusted and passed straight into an allocation before any content is validated.

### Finding Description
`extractBytes32Array` reads the length word and allocates immediately with no bound check at all: [1](#0-0) 

`extractBytesArray` performs a weak header-offset check but not a data-size/`len` sanity check before allocating: [2](#0-1) 

`extractSigArray` follows the identical unchecked-`len` pattern: [3](#0-2) 

In every case, `len` comes from `words[offset].intValueSafe()` — a 256-bit `DataWord` taken from the calldata sent to the precompile — and is used directly as `new byte[len][]`. Because `intValueSafe()` can return any value up to `Integer.MAX_VALUE`, a single 32-byte word in the calldata can force the JVM to allocate an object array of up to ~2^31 references (object-array overhead alone is several GB), while the actual byte array `data` supplied to the precompile can be only a few hundred bytes long — analogous to the 6-byte crafted Analyze/NIfTI header in the CImg report producing a ~1.3GB allocation. This is directly reachable by any account that can invoke a smart contract, since the precompile is called via ordinary `CALL`/`STATICCALL` opcodes at address `0x9` when `VMConfig.allowTvmSolidity059()` is enabled, as shown by the dispatch table: [4](#0-3) 

### Impact Explanation
Unlike EVM memory opcodes (`CODECOPY`, `CALLDATACOPY`, `EXTCODECOPY`), whose destination-size allocations are gated by `EnergyCost.calcMemEnergy`, which charges quadratic memory-expansion energy proportional to the requested size before the copy executes (see `getCodeCopyCost`/`getCallDataCopyCost`), the precompile's internal `len`-derived array allocation happens inside `execute()` after the (typically much smaller, calldata-size-based) precompile energy charge and is not covered by TVM's per-byte memory-expansion metering. A caller can therefore trigger an allocation whose cost is decoupled from the energy actually paid, potentially exhausting node heap memory (`OutOfMemoryError`) and crashing or degrading availability of any full/witness node that executes the transaction — a node crash/halt impact.

### Likelihood Explanation
The path is reachable by any unprivileged account: deploy or call an existing contract that issues a `CALL`/`STATICCALL` to precompile address `0x9` (`BatchValidateSign`) with crafted calldata containing a small buffer but an oversized length word. No special privileges, signatures beyond a normal transaction, or SR/witness status are required. The relevant feature flag `allowTvmSolidity059` is a standard mainnet-enabled feature, and `BatchValidateSign` is a documented, publicly callable precompile.

### Recommendation
In `extractBytes32Array`, `extractBytesArray`, and `extractSigArray`, validate `len` against the actual bounds of `words`/`data` (e.g., `len >= 0` and `offset + 1 + len <= words.length`, and that `data.length` is large enough to contain `len` fixed-size records) before allocating the result array, throwing a `Program.Exception`/returning an energy-exhausted result on failure rather than allocating first.

### Proof of Concept
1. Craft a transaction that calls a contract which issues `STATICCALL`/`CALL` to precompile address `0x0000000000000000000000000000000000000000000000000000000000000009` (`BatchValidateSign`).
2. Build the calldata so that the array-length word located at the `signatures`/`addresses` array's length offset (parsed via `extractBytesArray`/`extractBytes32Array`) is set to a very large value such as `0x7FFFFFFF`, while the actual `data` byte array passed is only a few hundred bytes.
3. When `PrecompiledContracts.execute` for `BatchValidateSign` calls `extractBytesArray`/`extractBytes32Array`, `len` is read from the crafted word and `new byte[len][]` is allocated immediately, consuming multiple GB of heap for a call whose calldata is only hundreds of bytes, before any validation that the array actually fits within `data`.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L254-256)
```java
    if (VMConfig.allowTvmSolidity059() && address.equals(batchValidateSignAddr)) {
      return batchValidateSign;
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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L414-426)
```java
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
