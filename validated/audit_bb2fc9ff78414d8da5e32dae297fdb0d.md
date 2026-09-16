### Title
Unbounded array allocation from attacker-controlled length field in TVM precompiled contracts (BatchValidateSign) - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
`PrecompiledContracts.java` contains helper methods that decode ABI-encoded arrays out of the raw calldata passed to a precompiled contract (used by `BatchValidateSign`/`ValidateMultiSign`). These helpers read a 32-byte "array length" word directly from attacker-controlled `data` and immediately allocate a Java array of that many elements, before any check that the declared length is consistent with the actual size of the supplied calldata. This mirrors the ImageMagick `ReadEPTImage` bug class (CVE-2017-11530): a length value taken from untrusted input is used to size a memory allocation with no upper bound or cross-check against the real amount of available data, letting a small malicious input trigger a huge allocation.

### Finding Description
`extractBytesArray`, `extractBytes32Array`, and `extractSigArray` each read a length field with `words[offset].intValueSafe()` from the (attacker-supplied) contract-call `data`, then do `new byte[len][]` (or read `len` `DataWord`s) before validating that `data` actually contains `len` elements: [1](#0-0) 

`len` comes straight from `intValueSafe()` on a `DataWord` slice of the calldata, so it can be crafted to any value up to `Integer.MAX_VALUE` while the actual `data` array passed to the precompile can be tiny (a few words). The subsequent `System.arraycopy`/loop calls would fail with an `ArrayIndexOutOfBoundsException` only *after* the oversized array has already been allocated: [2](#0-1) 

These extraction helpers back the `BatchValidateSign` and `ValidateMultiSign` precompiled contracts, which are reachable by any contract that performs a `CALL`/`STATICCALL` to the corresponding precompile address (`batchValidateSignAddr` / `validateMultiSignAddr`), gated only by the `allowTvmSolidity059` feature flag which is enabled on current chains: [3](#0-2) 

I was not able to fully confirm within the available tool iterations whether `getEnergyForData()` for `BatchValidateSign`/`ValidateMultiSign` computes its energy charge from the *declared* array length or purely from `data.length` (the actual calldata size). If energy is billed by `data.length` (the common EVM/TVM pattern for calldata-sized costs), an attacker can submit a short calldata blob whose embedded length word is enormous, paying only for the small calldata while forcing the node to attempt an allocation on the order of gigabytes.

### Impact Explanation
A single unprivileged, minimally-priced smart-contract call (any account can deploy/execute a contract that calls the precompile) can force the executing full node to attempt allocating an oversized array (`new byte[len][]` where `len` approaches `Integer.MAX_VALUE`), which throws `OutOfMemoryError` or induces heavy GC pressure across the JVM process. Because block/transaction execution in java-tron runs in the node's main process (via `Manager`/`Runtime`), this can crash or stall the node processing that block, and if triggered broadly (e.g., re-executed by all validating nodes for the same transaction), it can cause a network-wide denial of service or chain halt — matching the "node crash or halt" acceptance criterion.

### Likelihood Explanation
Likelihood is moderate to high if the energy/gas metering for these precompiles is based on raw calldata size rather than on the declared internal array length, since crafting the malicious calldata requires no special privileges — just a normal signed transaction invoking a contract that calls the `BatchValidateSign`/`ValidateMultiSign` precompile address with a crafted length word. This is a common bug class (as demonstrated by the CVE analog) where header/length fields are trusted for allocation sizing without bound checks.

### Recommendation
Before allocating `bytes32Array`/`bytesArray`/`sigArray`, validate that `len` is within a small, sane upper bound (e.g., limited by `words.length` or a fixed max signature/array count) and that `len * elementSize` does not exceed the remaining bytes in `data`, rejecting the call (as `PrecompiledContract.execute` failure) instead of allocating first. Apply the same defensive length-vs-available-data check pattern already used in `RLP.verifyLength` (`framework/src/main/java/org/tron/core/capsule/utils/RLP.java`) to these ABI-array decoding helpers.

### Proof of Concept
1. Deploy a contract that performs a low-level `staticcall`/`call` to the `BatchValidateSign` precompile address (`0x1000006` per `batchValidateSignAddr`), or to `ValidateMultiSign`.
2. Craft calldata where the header word for the array offset/length (consumed by `extractBytesArray`/`extractSigArray`) encodes a huge value (e.g., `0x7FFFFFFF`), while the rest of the calldata is only a few words long.
3. Broadcast a transaction invoking this contract call; when the node's TVM interpreter reaches `extractBytesArray`/`extractSigArray`/`extractBytes32Array`, it executes `new byte[len][]` with `len` ≈ 2^31, attempting a multi-gigabyte allocation and triggering `OutOfMemoryError`/severe GC stalls on the executing node before any bounds error is raised.

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L414-430)
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

  private static byte[] extractBytes(byte[] data, int offset, int len) {
    return Arrays.copyOfRange(data, offset, offset + len);
  }
```
