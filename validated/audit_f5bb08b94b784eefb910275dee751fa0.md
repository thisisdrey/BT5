### Title
Unbounded array-length field from precompile calldata drives unchecked allocation, enabling node crash (DoS) - ([File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java])

### Summary
`extractBytesArray`, `extractBytes32Array`, and `extractSigArray` in `PrecompiledContracts.java` read an array-count field directly from calldata and use it to size an array allocation with no upper bound, mirroring the CVE-2017-1000229 pattern where a length field parsed from external input is used to size a buffer/allocation without validation.

### Finding Description
`extractBytes32Array` and `extractBytesArray`/`extractSigArray` take `len = words[offset].intValueSafe()` — a value taken directly from the calldata of a transaction calling the precompile — and immediately allocate `new byte[len][]` with no upper-bound or sanity check: [1](#0-0) 

Compare this to `PrecompiledContracts.ModExp`, which explicitly bounds attacker-controlled lengths (`UPPER_BOUND = 1024`) before use: [2](#0-1) 

These `extract*Array` helpers are used by the `BatchValidateSign` and `ValidateMultiSign` precompiled contracts, which are reachable from any TVM `CALL`/`STATICCALL` to their fixed precompile addresses once `VMConfig.allowTvmSolidity059()` is enabled: [3](#0-2) 

Because `intValueSafe()` on a `DataWord` clamps unrepresentable 256-bit values (it does not reject them), an attacker can encode a "count" word as a huge value (e.g., close to `Integer.MAX_VALUE`) in the calldata passed to these precompiles. This directly drives `new byte[len][]` or `new byte[len]` allocations with attacker-chosen `len`, which can trigger `OutOfMemoryError`/`NegativeArraySizeException` inside the TVM interpreter thread — the same root-cause shape as the optipng bug (an unvalidated length field taken from external input used to size a buffer, causing DoS/crash).

### Impact Explanation
A successful trigger causes an unhandled `OutOfMemoryError` (which does not necessarily get caught by TVM's checked-exception paths designed for `Program.Exception`/`ContractValidateException`) or a large, uncontrolled allocation inside a node processing a single broadcast transaction. This can crash or destabilize the executing full node / SR node when the transaction is included and re-executed by every node, matching the "node crash or halt" acceptance criterion.

### Likelihood Explanation
Reaching this requires only crafting a smart-contract call to the `BatchValidateSign` (0x66) or `ValidateMultiSign` precompile address with a manipulated array-length word in calldata — no special privileges, keys, or SR/witness status needed, just a signed transaction from any account. `VMConfig.allowTvmSolidity059()` must be enabled, which is the case on current mainnet/most networks running Solidity 0.5.9-compatible TVM.

### Recommendation
Add an explicit upper bound (mirroring `ModExp.UPPER_BOUND`) on `len` in `extractBytesArray`, `extractBytes32Array`, and `extractSigArray` before allocating `byte[len][]`/`byte[len]`, and validate that `len` is non-negative and consistent with the remaining `data`/`words` length before allocation, returning an execution failure (`Pair.of(false, ...)`) instead of allocating unbounded memory.

### Proof of Concept
Not independently executed in this analysis (no execution environment available). Conceptually: construct a `TriggerSmartContract` transaction whose contract calls the `BatchValidateSign` or `ValidateMultiSign` precompile address with calldata where the array-length word (the value read via `words[offset].intValueSafe()`) is set to a very large value (e.g., `0x7FFFFFFF`), broadcast the transaction, and observe the executing node's TVM interpreter attempt `new byte[0x7FFFFFFF][]`, resulting in `OutOfMemoryError`/crash.

**Caveat:** I could not retrieve the exact call sites within `BatchValidateSign`/`ValidateMultiSign` classes that invoke `extractBytesArray`/`extractBytes32Array`/`extractSigArray` (the read tool did not return the full contents before the session ended), so the precise field layout consumed by those precompiles is inferred from the helper signatures and test file names (`BatchValidateSignContractTest.java`, `ValidateMultiSignContractTest.java`) rather than fully confirmed end-to-end. A Devin session with full file access would be needed to confirm the exact calldata offsets and whether any implicit bound exists earlier in the call chain (e.g., via `isValidAbiEncoding`).

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L648-700)
```java
    private static final int UPPER_BOUND = 1024;

    private static final long MIN_ENERGY_TIP7883 = 500L;

    private static final BigInteger MIN_ENERGY_TIP7883_BI =
        BigInteger.valueOf(MIN_ENERGY_TIP7883);

    @Override
    public long getEnergyForData(byte[] data) {

      if (data == null) {
        data = EMPTY_BYTE_ARRAY;
      }

      int baseLen = parseLen(data, 0);
      int expLen = parseLen(data, 1);
      int modLen = parseLen(data, 2);


      byte[] expHighBytes = parseBytes(data, addSafely(ARGS_OFFSET, baseLen), min(expLen, 32,
          VMConfig.disableJavaLangMath()));

      if (VMConfig.allowTvmOsaka()) {
        return getEnergyTIP7883(baseLen, modLen, expHighBytes, expLen);
      }

      long multComplexity = getMultComplexity(max(baseLen, modLen, VMConfig.disableJavaLangMath()));
      long adjExpLen = getAdjustedExponentLength(expHighBytes, expLen);

      // use big numbers to stay safe in case of overflow
      BigInteger energy = BigInteger.valueOf(multComplexity)
          .multiply(BigInteger.valueOf(max(adjExpLen, 1, VMConfig.disableJavaLangMath())))
          .divide(GQUAD_DIVISOR);

      return isLessThan(energy, BigInteger.valueOf(Long.MAX_VALUE)) ? energy.longValueExact()
          : Long.MAX_VALUE;
    }

    @Override
    public Pair<Boolean, byte[]> execute(byte[] data) {

      if (data == null) {
        return Pair.of(true, EMPTY_BYTE_ARRAY);
      }

      int baseLen = parseLen(data, 0);
      int expLen = parseLen(data, 1);
      int modLen = parseLen(data, 2);

      if (VMConfig.allowTvmOsaka()
          && (baseLen > UPPER_BOUND || expLen > UPPER_BOUND || modLen > UPPER_BOUND)) {
        return Pair.of(false, EMPTY_BYTE_ARRAY);
      }
```
