### Title
Unbounded array allocation from attacker-controlled length in TVM precompiled contract signature/array extraction helpers - ([File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java])

### Summary
`PrecompiledContracts.extractBytes32Array`, `extractBytesArray`, and `extractSigArray` read an array-length value directly out of attacker-supplied TVM call data and use it, without any upper-bound check, to allocate a Java array (`new byte[len][]`). This mirrors the Excelize bug class: an untrusted length field taken from input is used for allocation sizing without enforcing a sane maximum, letting a small, cheap call trigger an oversized allocation.

### Finding Description
In `extractBytes32Array`:
```
private static byte[][] extractBytes32Array(DataWord[] words, int offset) {
  int len = words[offset].intValueSafe();
  byte[][] bytes32Array = new byte[len][];
  ...
``` [1](#0-0) 

and in `extractBytesArray`/`extractSigArray`: [2](#0-1) 

`len` comes straight from `words[offset].intValueSafe()`, i.e. a 256-bit word supplied as part of the precompile's `data` argument, which is fully controlled by the caller of the TVM `CALL`/`STATICCALL` opcode that targets the precompiled-contract address. Unlike `ModExp.execute`, which explicitly enforces `UPPER_BOUND = 1024` on `baseLen`/`expLen`/`modLen` before use: [3](#0-2) 

these three array-extraction helpers have no equivalent bound. Only a check that `offset` itself is within the `words` array is performed (`if (offset > words.length - 1) return new byte[0][];`), but the extracted `len` value itself is never clamped to a small maximum before it is used to size `new byte[len][]`. A caller can therefore craft calldata whose length-word decodes (via `intValueSafe()`) to a very large positive `int` (up to `Integer.MAX_VALUE`), forcing the JVM to attempt allocating an array of that many `byte[]` references before any subsequent per-element bounds checks or energy-cost accounting for the loop body take effect.

### Impact Explanation
Allocation of a huge `byte[][]` triggers `OutOfMemoryError` or extreme GC pressure on the node processing the transaction/contract call. Because this happens inside contract execution invoked from a `CALL` to the precompiled address, any account (unprivileged) can reach it by crafting a smart-contract call with this precompile as target and adversarial "length" words in the input data. If the resulting `OutOfMemoryError`/`NegativeArraySizeException` propagates out of the intended `try/catch` scope of the calling precompile logic, it can degrade or crash the executing node, effectively a resource-exhaustion / node-availability issue triggerable by a single transaction.

### Likelihood Explanation
Reaching these helper functions requires locating which precompiled contract(s) actually invoke `extractBytes32Array`/`extractBytesArray`/`extractSigArray` (e.g., multi-signature validation type precompiles) and confirming the exact call path from a `CALL` opcode's `data` argument to `words[offset]`. I was not able to conclusively identify and verify the direct caller(s) of these three helpers before running out of search iterations, nor confirm whether the outer `execute()` methods that use them wrap the byte[][] allocation in an energy-aware pre-check or a broad `try/catch(Throwable)` (as seen in `VerifyMintProof`/`VerifyTransferProof`) that would contain the failure instead of crashing the node. This uncertainty affects confidence in the final severity: if the calling precompile already validates `data.length` tightly bounds `len` before reaching these helpers, exploitability would be much lower.

### Recommendation
Add an explicit upper bound check on `len` in `extractBytes32Array`, `extractBytesArray`, and `extractSigArray` immediately after reading `words[offset].intValueSafe()`, analogous to `ModExp.UPPER_BOUND`, and reject (return empty/failure) when `len` exceeds a small sane maximum (e.g., bounded by the actual remaining `data`/`words` length) before allocating any array.

### Proof of Concept
Not fully constructed: doing so requires identifying the specific precompiled-contract entry point(s) that call these three helpers and confirming their `execute()` wraps calls without an equivalent upper-bound check, which I could not verify within the available search budget.

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L399-426)
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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L697-700)
```java
      if (VMConfig.allowTvmOsaka()
          && (baseLen > UPPER_BOUND || expLen > UPPER_BOUND || modLen > UPPER_BOUND)) {
        return Pair.of(false, EMPTY_BYTE_ARRAY);
      }
```
