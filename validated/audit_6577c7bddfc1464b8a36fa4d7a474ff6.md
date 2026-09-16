### Title
Unbounded array allocation from attacker-controlled length field in `extractSigArray` enables memory-exhaustion DoS - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
`PrecompiledContracts.extractSigArray` reads a "signature count" directly from the calldata words passed into a TVM precompiled contract and immediately uses it to size a `byte[][]` array, with no upper bound applied before the allocation. This mirrors the CVE-2018-16645 pattern (a crafted header length field driving an oversized allocation before validation), except the trigger here is TVM precompile calldata reachable from any signed transaction that calls the precompile address.

### Finding Description
`extractSigArray` takes the `len` value straight from `words[offset].intValueSafe()`: [1](#0-0) 

`intValueSafe()` clamps a 256-bit `DataWord` into a Java `int`, but it does not constrain the value to any sane maximum signature count — an attacker fully controls this word via ABI-encoded calldata to the precompile. The result is passed directly to `new byte[len][]` with no check that `len` is small, non-negative in a meaningful sense, or consistent with the actual size of `data`. Only after the array is allocated does the loop attempt to read signature bytes via `extractBytes`, which itself performs an unchecked `Arrays.copyOfRange` and can throw only after the (potentially huge) array has already been allocated.

This is structurally the same bug class as `ReadBMPImage`/`ReadDIBImage` in ImageMagick: a length/count field taken from untrusted input is used to size a large allocation before the input is otherwise validated, producing an out-of-memory condition.

### Impact Explanation
A crafted calldata word with a very large integer value causes the JVM to attempt allocating a correspondingly large `byte[][]` (or fail with `OutOfMemoryError`) inside precompile execution triggered by an ordinary transaction. Because precompile execution happens inside the node's transaction-processing/TVM path, a burst of such calls can exhaust heap memory across concurrent executions, leading to node instability, crashes, or degraded ability to process/serve further transactions — a Medium-severity node crash/DoS condition, consistent with the CVSS profile of the reference CVE (availability impact, no confidentiality/integrity impact, low complexity, requires only sending a transaction).

### Likelihood Explanation
Reachability requires only a standard signed transaction invoking the corresponding precompiled contract address with attacker-chosen calldata — this is available to any unprivileged contract deployer or caller, with no special permissions needed. The precondition is that energy metering for the precompile does not already reject the oversized count before `extractSigArray` runs; this could not be confirmed from the code inspected, since the exact `getEnergyForData`/pre-validation logic for the caller of `extractSigArray` was not located in the excerpts reviewed. This is a material uncertainty: if an upstream check already caps the signature count (e.g., to 5, matching TRON's max multi-sig keys) before `extractSigArray` is invoked, the practical exploitability of this specific path would be eliminated or greatly reduced.

### Recommendation
Add an explicit upper bound check on `len` in `extractSigArray` (e.g., reject if `len < 0` or `len` exceeds the maximum number of signatures the protocol supports, such as 5) before allocating `bytesArray`, and validate that `offset + len` stays within the bounds of `words`/`data` prior to allocation, mirroring the defensive length checks already used elsewhere in this file's `isValidAbiEncoding`.

### Proof of Concept
Construct a transaction that calls the precompiled contract address associated with `extractSigArray` (the batch/multi-signature validation precompile) with ABI-encoded calldata where the word at the expected "count" offset is set to a very large value (e.g., `0x7FFFFFFF`). When `PrecompiledContract.execute` reaches `extractSigArray`, `len = words[offset].intValueSafe()` evaluates to this large value and `new byte[len][]` is attempted, causing an `OutOfMemoryError` or large heap spike during transaction execution.

Note: I was unable to fully verify, within the available search iterations, whether the precompile that calls `extractSigArray` applies an upstream bound on the count word (via its `getEnergyForData` or an explicit `cnt` check) before reaching this allocation; if such a bound already exists, the exploitability of this specific code path is reduced. This should be confirmed by reviewing the full call sites of `extractSigArray` and the corresponding `getEnergyForData`/`execute` implementation in `PrecompiledContracts.java`.

### Citations

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
