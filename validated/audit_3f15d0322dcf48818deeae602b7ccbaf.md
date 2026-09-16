### Title
Unbounded array allocation from attacker-controlled length in TVM precompiled-contract ABI decoding causes node OutOfMemoryError - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
`PrecompiledContracts` decodes dynamic-array parameters for precompiled contracts (e.g. multi-signature validation) by reading a length word directly from the caller-supplied calldata and immediately allocating a Java array of that size, with no upper bound check analogous to the unchecked `CryptoVec` growth described in the report.

### Finding Description
`extractBytes32Array`, `extractBytesArray`, and `extractSigArray` each read a length from an attacker-controlled `DataWord` word and use it directly as the size of a newly allocated array: [1](#0-0) [2](#0-1) [3](#0-2) 

The length comes from `DataWord.intValueSafe()`, which is the only guard applied: [4](#0-3) 

`intValueSafe()` only prevents a Java `int` overflow — if the attacker supplies a word occupying more than 4 bytes (e.g. `0xFFFFFFFF...`), the function returns `Integer.MAX_VALUE` rather than throwing or clamping to a sane protocol-level limit. That value is then used directly as `new byte[len][]` or `new byte[len]` in the extraction helpers above, i.e. an attempt to allocate on the order of billions of array slots (`Integer.MAX_VALUE` references), which the JVM cannot satisfy and throws `OutOfMemoryError`.

This mirrors the root cause in the external report: a peer/caller-controlled length field is used to size a buffer/array before any sanity check against a maximum, coupling untrusted input directly to unchecked allocation.

### Impact Explanation
`PrecompiledContracts` implements TVM precompiles that are reachable by any smart-contract `CALL`/`STATICCALL` to the fixed precompile addresses (e.g. multi-sig / batch-signature-validation precompiles that use these array extractors), which in turn is reachable from any signed transaction that triggers a contract making such a call. Triggering `OutOfMemoryError` inside VM execution is an `Error`, not an `Exception`; unless the surrounding execution/actuator layer specifically catches `Throwable`/`Error` (only `recoverAddrBySign` in this file is shown catching `Throwable`), the error can propagate out of the transaction-processing path. Depending on how the JVM and heap are shared with other execution threads, this can degrade or crash the node process, i.e. a self-inflicted resource-exhaustion / halt condition triggerable by a single crafted transaction — matching the accepted "node crash or halt" impact class.

### Likelihood Explanation
Any account can submit a `TriggerSmartContract` transaction (or deploy a contract that immediately calls the precompile) with calldata engineered so that the length word passed to `extractBytes32Array`/`extractBytesArray`/`extractSigArray` decodes to `Integer.MAX_VALUE` via `intValueSafe()`. No special privileges, signatures, or specific network conditions are required — only a single, cheaply constructed transaction, making this trivially and repeatably reachable by an unprivileged transaction broadcaster.

### Recommendation
Validate decoded array lengths against a protocol-appropriate maximum (matching the realistic bound implied by `data.length`/`WORD_SIZE`) before allocating `bytes32Array`/`bytesArray`/`spendCv`-style arrays in `PrecompiledContracts.java`, rejecting the call (or charging/erroring gracefully) instead of allocating directly from `intValueSafe()`. Additionally, ensure the TVM execution boundary that invokes precompiled contracts catches `OutOfMemoryError` (or generic `Throwable`) and converts it into a normal, revert-style VM exception rather than allowing it to propagate to the JVM.

### Proof of Concept
Construct calldata for a precompiled contract call (any that uses `extractBytesArray`/`extractBytes32Array`/`extractSigArray`, e.g. the multi-sig validation precompile) where the ABI "array length" word at the expected `offset` is set to a value such as `0xFFFFFFFF00000000...` (occupying more than 4 significant bytes so `bytesOccupied() > 4`). `intValueSafe()` returns `Integer.MAX_VALUE`, and the subsequent `new byte[len][]` allocation attempt throws `OutOfMemoryError` during precompile execution triggered by a single `TriggerSmartContract` transaction.

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
