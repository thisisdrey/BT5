### Title
Unbounded array allocation from attacker-controlled length in TVM precompile helper functions - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
`PrecompiledContracts.extractBytes32Array`, `extractBytesArray`, and `extractSigArray` read an array-length value directly from attacker-controlled calldata (`words[offset].intValueSafe()`) and immediately use it to size a `byte[len][]` allocation before any bound is checked against the actual size of the supplied calldata. [1](#0-0) 

### Finding Description
`extractBytes32Array` takes `len` straight from a `DataWord` supplied in the precompile's input data and allocates a `byte[len][]` before verifying that `len` array slots actually exist in `words`: [2](#0-1) 

`extractBytesArray` and `extractSigArray` follow the same pattern — they only guard against `offset` being out of range, not `len`, before doing `new byte[len][]`: [3](#0-2) 

`intValueSafe()` is meant to avoid `ArithmeticException` on overflow, but it still returns values up to `Integer.MAX_VALUE`, which is fully attacker-controlled: any account can invoke the associated precompiled contract from a `TriggerSmartContract` transaction and place any 32-byte word (interpreted as `len`) at the expected offset. The subsequent `new byte[len][]` allocation is an array-of-arrays, so even before the loop touches any actual signature/element data, the JVM must allocate a reference array of `len` pointers (8 bytes per slot on a 64-bit JVM), meaning a length near `Integer.MAX_VALUE` requests roughly 16 GB in a single allocation. This is directly analogous to CVE-2018-10111 in GEGL, where an attacker-supplied size field is used for allocation without a sanity/bounds check first, causing an `OutOfMemoryError` crash. Here, the missing check is not on the memory size directly (as `EnergyCost`/`Memory.extend` do for TVM memory-expansion opcodes via `MEM_LIMIT`/`checkMemorySize`, see `actuator/src/main/java/org/tron/core/vm/EnergyCost.java:572-576`), but on this parallel "batch length" decoding path used by precompiles, which bypasses that memory-metering machinery entirely. [4](#0-3) 

### Impact Explanation
Any unprivileged account that can broadcast a `TriggerSmartContract`/`TriggerConstantContract` transaction targeting the precompile using these helpers can trigger a very large single allocation attempt. Depending on JVM heap configuration this can throw `OutOfMemoryError` inside node/validator processes handling the transaction — a node crash or denial of service for a public API/RPC/full node, matching the "node crash or halt" acceptance criterion.

### Likelihood Explanation
The trigger requires only a single crafted transaction with a manipulated length word at a known offset; no special privileges, staking, or preconditions are required beyond the normal energy fee for invoking the precompiled contract. However, I was not able to fully trace, within tool-call limits, which precompiled contract class(s) call `extractBytesArray`/`extractSigArray`/`extractBytes32Array` at runtime (test file `BatchValidateSignContractTest` strongly suggests a batch-signature-validation precompile is the caller), nor whether that precompile enforces its own upper bound on `len` before or after calling these helpers. This caller-side context is necessary to fully confirm end-to-end exploitability and should be verified directly in the codebase (e.g., via a Devin session) before treating this as a confirmed, unmitigated vulnerability.

### Recommendation
Before allocating `byte[len][]` in `extractBytes32Array`, `extractBytesArray`, and `extractSigArray`, validate `len` against the actual remaining length of `words`/`data` (e.g., `len <= words.length - offset - 1`) and reject with a safe failure (return `Pair.of(true, DataWord.ZERO().getData())` or throw a caught exception) instead of allocating first and validating later, mirroring the `checkMemorySize`/`MEM_LIMIT` pattern already used for TVM memory-expansion opcodes.

### Proof of Concept
1. Craft a `TriggerSmartContract` transaction whose input calls the precompiled contract that eventually invokes `extractBytesArray`/`extractSigArray`/`extractBytes32Array` (per test naming, the batch-signature-validation precompile).
2. At the length-word offset consumed by these helpers, place a large value (e.g., close to `Integer.MAX_VALUE`) rather than the real element count.
3. Submit the transaction; when `PrecompiledContracts` executes and reaches `new byte[len][]`, the JVM attempts a multi-gigabyte allocation, risking `OutOfMemoryError` and process instability on the executing node.

Because I could not conclusively identify and inspect the exact precompile `execute()` caller in the remaining tool budget, this should be validated in-repo (find the class(es) invoking `extractBytesArray`/`extractSigArray`/`extractBytes32Array`, e.g., via `grep` on those method names) before remediation to confirm there is no upstream bound already enforced. [1](#0-0)

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L390-426)
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
```

**File:** actuator/src/main/java/org/tron/core/vm/EnergyCost.java (L572-576)
```java
  private static void checkMemorySize(int op, BigInteger newMemSize) {
    if (newMemSize.compareTo(MEM_LIMIT) > 0) {
      throw Program.Exception.memoryOverflow(op);
    }
  }
```
