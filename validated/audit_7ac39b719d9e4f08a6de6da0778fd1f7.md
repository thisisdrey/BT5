## Finding: Unbounded array allocation from attacker-controlled length word in TVM precompiled contracts

### Title
Unvalidated attacker-controlled length allows unbounded array allocation / OOM in TVM precompiled-contract call-data parsers - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The CVE analog is the same bug class as CVE-2017-15193: a length value taken directly from untrusted input is used to size a memory allocation with no upper-bound check before the allocation is attempted. In java-tron, the private helpers `extractBytes32Array`, `extractBytesArray`, and `extractSigArray` in `PrecompiledContracts.java` read a 256-bit `DataWord` from TVM call data, convert it with `intValueSafe()`, and immediately allocate an array of that size. [1](#0-0) [2](#0-1) [3](#0-2) 

### Finding Description
`extractBytes32Array(words, offset)` computes `len = words[offset].intValueSafe()` and then executes `new byte[len][]` before validating that `len` is consistent with the actual amount of call data provided. `extractBytesArray` and `extractSigArray` follow the identical pattern. `intValueSafe()` on a `DataWord` clamps an arbitrary 256-bit big-endian value down into the `int` range, so a caller can trivially craft a 32-byte word whose value is `0x7FFFFFFF` (or any large positive int), causing `len` to be an attacker-chosen value up to `Integer.MAX_VALUE` regardless of how small the rest of the actual call-data payload is.

This mirrors the Wireshark MBIM dissector flaw: a length field taken from packet/input data drives a memory allocation with no sanity bound relative to the size of the actual buffer, allowing a single small malicious input to trigger an allocation many orders of magnitude larger than the input itself, exhausting heap memory.

These helpers are used to decode the calldata for the `BatchValidateSign` (address `0x...09`) and `ValidateMultiSign` (address `0x...0a`) precompiled contracts, both of which are reachable from any TVM contract call made through an ordinary `TriggerSmartContract`/`CreateSmartContract` transaction — i.e., by any unprivileged transaction broadcaster, with no special permission required. [4](#0-3) [5](#0-4) 

### Impact Explanation
An attacker can submit a smart-contract call whose input data is only a few dozen bytes but encodes a huge "array length" word at the offset consumed by `extractBytes32Array`/`extractBytesArray`/`extractSigArray`. Because the array is sized and allocated (`new byte[len][]`) before the code attempts to read any of the (nonexistent) subsequent words, the JVM will attempt to allocate an array of up to `Integer.MAX_VALUE` references. On a 64-bit JVM this is tens of gigabytes of memory for a single allocation. This can throw `OutOfMemoryError`, which — unlike a checked `Exception` — is not guaranteed to be caught by ordinary VM exception-handling paths in the interpreter, and can destabilize or crash the node process. Because this reachable from every node that executes the transaction (all full nodes, SRs, and any node that re-executes/validates the block), a single crafted transaction can be used to attempt a denial-of-service against the whole network, which is squarely in the "node crash or halt" impact category called out by the validation criteria.

### Likelihood Explanation
Likelihood is high for reachability: any account can build and broadcast a `TriggerSmartContract` (or trigger it via a contract call from another contract) that targets precompile address `0x09` or `0x0a` with attacker-controlled calldata; no special permission, ownership, or SR/witness role is required. The energy-metering step for these precompiles does not intervene before the vulnerable allocation executes inside `execute()`, since the parsing/allocation happens as part of `execute()` itself.

### Recommendation
Before allocating `bytes32Array`/`bytesArray`/`sigArray` in `extractBytes32Array`, `extractBytesArray`, and `extractSigArray`, validate that `len` is consistent with the remaining length of `data`/`words` (e.g., `len * WORD_SIZE` must not exceed the available call-data length, and `offset + len + 1` must not exceed `words.length`), returning an execution failure (`Pair.of(false, EMPTY_BYTE_ARRAY)`) instead of allocating when the declared length is inconsistent with the actual buffer size — mirroring the approach already used in `RLP.calcLength`/`verifyLength` elsewhere in the codebase. [6](#0-5) 

### Proof of Concept
1. Craft a `TriggerSmartContract` transaction whose target/CALL invokes precompiled contract address `0x0000000000000000000000000000000000000000000000000000000000000009` (`BatchValidateSign`) or `...0a` (`ValidateMultiSign`).
2. Set the call data so that the `DataWord` read at the "array length" offset consumed by `extractBytes32Array`/`extractBytesArray` (see call sites in `PrecompiledContracts.java`) encodes a large positive integer (e.g., `0x000000000000000000000000000000000000000000000000000000007FFFFFFF`), while the actual remaining call data is only a few words long.
3. Broadcast the transaction; when the TVM executes the precompiled contract, `execute(data)` invokes the vulnerable helper, which performs `new byte[len][]` (or `new byte[len]`) with `len` near `Integer.MAX_VALUE`, attempting a multi-gigabyte allocation on every node that processes the transaction.

Note: I was not able to retrieve the full source of `BatchValidateSign.execute()`/`ValidateMultiSign.execute()` (only the helper methods and their call sites were confirmed) due to index truncation of this file section; a Devin session with full repository access should confirm the exact offset/word position used for `len` in each precompile's `execute()` to finalize the exact PoC calldata layout.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L149-152)
```java
  private static final DataWord batchValidateSignAddr = new DataWord(
      "0000000000000000000000000000000000000000000000000000000000000009");
  private static final DataWord validateMultiSignAddr = new DataWord(
      "000000000000000000000000000000000000000000000000000000000000000a");
```

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

**File:** framework/src/main/java/org/tron/core/capsule/utils/RLP.java (L605-617)
```java
  /**
   * Compares supplied length information with maximum possible
   *
   * @param suppliedLength Length info from header
   * @param availableLength Length of remaining object
   * @throws RuntimeException if supplied length is bigger than available
   */
  private static void verifyLength(int suppliedLength, int availableLength) {
    if (suppliedLength > availableLength) {
      throw new RuntimeException(String.format("Length parsed from RLP (%s bytes) is greater "
          + "than possible size of data (%s bytes)", suppliedLength, availableLength));
    }
  }
```
