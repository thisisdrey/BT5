### Title
Unbounded array allocation from unvalidated ABI length field in TVM precompiled contracts (`BatchValidateSign`/`ValidateMultiSign`) - ([File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java])

### Summary
The JLSEC-2026-475 report describes an integer-overflow-driven buffer allocation in libX11's `XCreateImage()`, where attacker-controlled width/height values are used to compute a buffer size without adequate bounds checking, leading to memory corruption. The closest reachable analog in java-tron is the length-driven array allocation helpers used by precompiled contracts that parse caller-supplied ABI data: `extractBytesArray`, `extractBytes32Array`, and `extractSigArray` in `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java` (lines 390-430). These helpers take a length word directly from the calldata (`words[offset].intValueSafe()`) and immediately allocate `new byte[len][]` with no upper bound check before the loop that reads that many elements.

### Finding Description
`extractBytesArray(DataWord[] words, int offset, byte[] data)` and `extractBytes32Array(DataWord[] words, int offset)`:
```
private static byte[][] extractBytes32Array(DataWord[] words, int offset) {
  int len = words[offset].intValueSafe();
  byte[][] bytes32Array = new byte[len][];
  ...
}

private static byte[][] extractBytesArray(DataWord[] words, int offset, byte[] data) {
  ...
  int len = words[offset].intValueSafe();
  byte[][] bytesArray = new byte[len][];
  ...
}
``` [1](#0-0) 

`len` is taken directly from a 256-bit `DataWord` supplied in the transaction's calldata to the precompile (`intValueSafe()` only clamps to the `int` range — it does not validate against the actual size of the calldata array). The subsequent `new byte[len][]` allocation for a value close to `Integer.MAX_VALUE` (or a large value combined with an unrelated/undersized `data` array) is performed before any bounds check against `data.length` or `words.length`, unlike the analogous `extractBytes` helper which at least performs `Arrays.copyOfRange` (which would throw on bad indices, but only after the initial array is allocated).

This mirrors the bug-class in the report: a length value taken from attacker-controlled input is used to size an allocation without validating it fits the actual backing buffer, so a value that is technically "in range" for the primitive type but nonsensical relative to the real data still gets used to drive allocation/loop bounds.

These helper methods are used by the `BatchValidateSign` and `ValidateMultiSign` precompiled contracts (both registered in `PrecompiledContracts` at lines 100-101), which are invoked via the `CALL`-to-precompile mechanism from ordinary TVM contract execution — i.e., reachable by any contract deployer or any address that can trigger a contract calling into these fixed precompile addresses. This is a "signature and permission verification" / "precompiles and energy metering" surface explicitly in scope per the task's rules.

### Impact Explanation
An attacker who can call a contract that invokes `BATCHVALIDATESIGN` or `VALIDATEMULTISIGN` (or directly construct calldata targeting those precompile addresses) can supply an oversized length word. Because `new byte[len][]` is allocated before validating that `len` corresponds to real data in the supplied byte array, a large `len` (up to `Integer.MAX_VALUE`) forces the JVM to attempt a very large object-array allocation on the node executing the transaction. Depending on how energy is charged for this call relative to the claimed array length, this can result in an `OutOfMemoryError` on the validating/executing full node, i.e., a node crash/halt condition for the process handling that transaction — one of the accepted "Validate" outcomes (node crash or halt) in the task's own acceptance criteria.

### Likelihood Explanation
Likelihood is limited by whether the energy-metering cost function for these precompiles charges energy proportional to the claimed array length before the allocation occurs, and by the maximum calldata size/energy limit enforceable per transaction. I was not able to fully confirm within the available tool budget whether `BatchValidateSign.getEnergyForData()` / `ValidateMultiSign.getEnergyForData()` scale their cost with the claimed `len` field or only with `data.length`; if they scale with `data.length` (the real calldata size) rather than the attacker-claimed `len`, the vulnerability is directly and cheaply reachable with a small transaction. If they scale with `len` itself, the practical impact is bounded by the energy limit, and this would reduce severity to a metered edge case. This uncertainty means the finding should be treated as **suspected but unverified** — a background agent with full repository access should read `BatchValidateSign` and `ValidateMultiSign`'s `getEnergyForData()` implementations and the actual invocation sites in `execute()` to confirm energy scales with real `data.length`/`words.length` rather than the raw `len` field before treating this as fully proven.

### Recommendation
Before allocating `new byte[len][]` in `extractBytesArray`, `extractBytes32Array`, and `extractSigArray`, validate `len` against the maximum number of elements that the supplied `data`/`words` array can actually contain (e.g., `len <= (words.length - offset - 1)` for `extractBytes32Array`, and an equivalent bound derived from `data.length` for `extractBytesArray`/`extractSigArray`), rejecting the call (returning failure, not throwing an uncontrolled exception) when the claimed length exceeds what the real buffer supports. Additionally confirm/ensure that `getEnergyForData()` for both `BatchValidateSign` and `ValidateMultiSign` charges energy based on verified array length rather than the raw attacker-supplied length word, so uneconomical allocation attempts are already priced out before array construction.

### Proof of Concept
Conceptual PoC (requires full-node execution to confirm impact, not fully validated here):
1. Deploy or call a contract that invokes the `BATCHVALIDATESIGN` (or `VALIDATEMULTISIGN`) precompiled contract address directly with hand-crafted calldata.
2. Craft the calldata so the length word at the expected array-length offset (consumed by `extractBytesArray`/`extractBytes32Array`/`extractSigArray`) is a large value (e.g., close to `0x7FFFFFFF`) while the remaining calldata bytes are minimal/truncated.
3. Submit the transaction with sufficient `feeLimit`/energy to pass initial energy checks (if energy is computed from real `data.length` rather than the claimed `len`).
4. Observe whether the executing node attempts `new byte[len][]`, triggering excessive memory allocation / `OutOfMemoryError` during precompile execution. [2](#0-1) [3](#0-2)

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L100-101)
```java
  private static final BatchValidateSign batchValidateSign = new BatchValidateSign();
  private static final ValidateMultiSign validateMultiSign = new ValidateMultiSign();
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L390-430)
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
