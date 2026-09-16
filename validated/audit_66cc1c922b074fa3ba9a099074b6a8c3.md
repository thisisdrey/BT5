### Title
Unbounded array-length field from precompile calldata causes uncontrolled memory allocation / node crash - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
The `BatchValidateSign` precompiled contract's calldata parsing helpers (`extractSigArray`, `extractBytesArray`, `extractBytes32Array`) read an array-length field directly from a single attacker-controlled 32-byte word and use it, unchecked against the actual size of the supplied calldata, to allocate Java arrays (`new byte[len][]`). This mirrors the CVE-2014-0075 pattern of trusting an attacker-supplied length field (Tomcat's chunk-size) without validating it against the real amount of available data, leading to uncontrolled resource consumption.

### Finding Description
`extractSigArray`, `extractBytesArray`, and `extractBytes32Array` all read `len` via `words[offset].intValueSafe()` and immediately allocate an array of that size: [1](#0-0) [2](#0-1) 

`intValueSafe()` only guards against values that don't fit in a `long`/negative sign — any value up to `Integer.MAX_VALUE` (0x7FFFFFFF) is returned verbatim, capped to `Integer.MAX_VALUE` only on true overflow: [3](#0-2) 

Unlike the `ModExp` precompile, which explicitly bounds `baseLen/expLen/modLen` against an `UPPER_BOUND` before use, these three helper methods perform no such bound check against the real calldata length before allocating `new byte[len][]`: [4](#0-3) 

A caller can craft calldata whose length-word encodes a huge value (e.g. `0x7FFFFFFF`), causing the JVM to attempt to allocate an array of that many object references (~16 GB for `Integer.MAX_VALUE` references), which throws `OutOfMemoryError`. `OutOfMemoryError` is an `Error`, not an `Exception`; ordinary `catch (Exception e)` guards around actuator/precompile execution will not catch it, so it can propagate and crash the executing thread/node.

### Impact Explanation
Any address able to call the TVM (via a smart-contract `CALL`/`STATICCALL` to the precompile address, or by deploying/invoking a contract that forwards calldata to it) can trigger a large, attacker-controlled allocation attempt on every full node and SR that processes the transaction, causing denial of service (node crash / halted block production) — a Medium/High impact analogous to CWE-400/CWE-190 in the reported Tomcat advisory.

### Likelihood Explanation
The precompile is reachable from any unprivileged account through a single signed transaction that calls a contract executing the relevant opcode/precompile address, requiring no special permission, stake, or witness role — the same "unprivileged transaction/contract call" reach required by the report's rules.

### Recommendation
Bound `len` in `extractSigArray`, `extractBytesArray`, and `extractBytes32Array` against the realistic maximum implied by `data.length` (e.g., `len <= (words.length - offset - 1)` or an explicit small upper bound, mirroring `ModExp`'s `UPPER_BOUND` check) before allocating any array, and validate that `offset + len` stays within `words.length` prior to allocation.

### Proof of Concept
Construct calldata for the `BatchValidateSign` precompile where the header word at the signature-array offset (consumed by `extractSigArray`/`extractBytesArray`) is set to `0x7FFFFFFF` instead of a legitimate small count, and submit it via a contract call (`TriggerSmartContract`) to the precompile address. When `execute()` invokes `extractSigArray`/`extractBytesArray`, `len = words[offset].intValueSafe()` returns `0x7FFFFFFF`, and `new byte[len][]` attempts a multi-gigabyte allocation, throwing `OutOfMemoryError` that is not caught by the surrounding `catch (Exception e)` handling, potentially crashing the node process handling the transaction.

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L693-704)
```java
      int baseLen = parseLen(data, 0);
      int expLen = parseLen(data, 1);
      int modLen = parseLen(data, 2);

      if (VMConfig.allowTvmOsaka()
          && (baseLen > UPPER_BOUND || expLen > UPPER_BOUND || modLen > UPPER_BOUND)) {
        return Pair.of(false, EMPTY_BYTE_ARRAY);
      }

      if (baseLen == 0 && modLen == 0 && expLen > UPPER_BOUND) {
        MUtil.checkCPUTimeForModExp();
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
