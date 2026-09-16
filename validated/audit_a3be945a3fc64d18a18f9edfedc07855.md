### Title
Unbounded array-length read from attacker-controlled call data can trigger `OutOfMemoryError` in TVM precompiles - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
Several TVM precompiled contracts (`BatchValidateSign`, `ValidateMultiSign`) parse ABI-encoded arrays out of raw contract call data using helper methods `extractBytes32Array`, `extractBytesArray`, and `extractSigArray`. These methods read an array-length word directly from the caller-supplied `DataWord[]` and use it, unchecked, to allocate a Java array (`new byte[len][]`), analogous to how the p11-kit RPC bug used an attacker-supplied length to size a buffer without validating it against the actual amount of data available.

### Finding Description
`extractBytes32Array`, `extractBytesArray`, and `extractSigArray` all take the "length" field as the raw `intValueSafe()` value of a `DataWord` supplied in the transaction's call data: [1](#0-0) 

`intValueSafe()` does not throw on out-of-range or malformed input — it clamps overflowing/negative values to `Integer.MAX_VALUE` rather than rejecting them: [2](#0-1) 

Consequently, an attacker who crafts calldata for a `CALL`/`STATICCALL` into `BatchValidateSign` or `ValidateMultiSign` (or any code path that calls these `extract*Array` helpers) can set the "array length" word to a value near `Integer.MAX_VALUE`. This causes an immediate attempt to allocate a `byte[Integer.MAX_VALUE][]` object array (a reference array of ~8–16 GB depending on JVM pointer size) before any bound is checked against the real length of `data`. There is no upfront validation (e.g., an `isValidAbiEncoding`-style check) inside these three helpers guaranteeing that `len` is consistent with `words.length`/`data.length`.

This mirrors the ALPINE-CVE-2020-29363 bug class: a deserializer trusts an attacker-supplied length field to size a buffer/array without first validating it is consistent with the actual payload size.

### Impact Explanation
A successful trigger causes the executing full node's JVM to attempt a massive heap allocation, resulting in `OutOfMemoryError`. Depending on how deep in the VM execution stack this propagates and how it's caught, this can manifest as a node crash, a stalled block-processing thread, or resource exhaustion affecting the whole node process — a denial-of-service against block production/serving that maps to "node crash or halt" in the accepted impact list. Because `BatchValidateSign`/`ValidateMultiSign` are precompiles reachable from ordinary smart-contract execution, any unprivileged account can trigger this by broadcasting a transaction that calls a contract invoking these precompiles with crafted calldata.

### Likelihood Explanation
Likelihood is high for triggering the code path (any transaction broadcaster can call a contract that invokes these precompiled addresses with attacker-chosen calldata), but I was not able to fully confirm from the retrieved code segments whether the top-level `execute()` methods of `BatchValidateSign`/`ValidateMultiSign` perform an `isValidAbiEncoding`-style pre-check that would reject an inconsistent length word before reaching `extractBytesArray`/`extractBytes32Array`/`extractSigArray`. This is a gap in my verification — the `PrecompiledContracts.java` execute() bodies for these two classes were not retrieved in the tool results, so I cannot state with certainty whether the huge-length path is actually reachable end-to-end or is filtered upstream.

### Recommendation
- Validate the parsed `len` in `extractBytes32Array`, `extractBytesArray`, and `extractSigArray` against `words.length` (and the corresponding `data.length`) before allocating `new byte[len][]`, rejecting the precompile call if the declared length is inconsistent with available data (similar to `isValidAbiEncoding`).
- Impose a hard sanity cap on `len` before allocation (e.g., bound by the max plausible number of signatures/items for these contracts) rather than trusting an ABI-supplied count directly.

### Proof of Concept
Not independently verified end-to-end due to inability to confirm the `execute()` entry points of `BatchValidateSign`/`ValidateMultiSign` guard against oversized length words before calling the vulnerable `extract*Array` helpers — recommend a Devin session with full repo access to trace `BatchValidateSign.execute()` / `ValidateMultiSign.execute()` and confirm whether crafted calldata (array-length word = `0xffffffff`) reaches `extractBytesArray`/`extractBytes32Array`/`extractSigArray` unguarded, then reproduce via a contract-call test in `framework/src/test/java/org/tron/common/runtime/vm/BatchValidateSignContractTest.java` / `ValidateMultiSignContractTest.java`.

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
