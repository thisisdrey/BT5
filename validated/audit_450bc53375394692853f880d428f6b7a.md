Based on my investigation, I found a genuine analog but cannot fully validate it within the remaining scope — I need to state findings honestly rather than overreach.

### Title
Missing bounds validation on attacker-controlled offsets in TVM precompile signature/byte-array extraction leads to unhandled exceptions / potential OOB read - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
`extractSigArray` and `extractBytesArray` in `PrecompiledContracts.java` compute byte offsets into the raw precompile `data` buffer directly from attacker-supplied `DataWord` values taken from the calldata, without validating that the resulting offset/length stays within `data.length` before calling `Arrays.copyOfRange`.

### Finding Description
`extractSigArray` reads a length word and, for each element, computes `bytesOffset` from calldata words and calls `extractBytes(data, (bytesOffset + offset + 2) * WORD_SIZE, SIG_LENGTH)`, which does `Arrays.copyOfRange(data, offset, offset + len)` with no upper/lower bound check against `data.length`. [1](#0-0) 
The sibling `extractBytesArray` has the identical pattern, also deriving `bytesLen` from attacker-controlled words. [2](#0-1) 
This mirrors the CVE-2018-20102 bug class: a length/offset value taken from untrusted input is used to slice a buffer without confirming it is validated against the buffer's actual bounds. In HAProxy this leaked uninitialized/adjacent stack memory; in this Java code, out-of-range offsets/lengths passed to `Arrays.copyOfRange` (which throws `ArrayIndexOutOfBoundsException`/`NegativeArraySizeException` for negative `fromIndex` or negative `len`) are not caught locally.

### Impact Explanation
Unlike C, Java bounds-checks array reads, so this cannot leak arbitrary process memory the way the HAProxy bug did. The concrete impact would be an uncaught runtime exception thrown out of the precompiled-contract `execute()` path. I was **not able to verify** within the available searches whether the caller of these precompiles (the `batchvalidatesign`/`validatemultisign` precompile dispatch and the TVM `CALL` opcode handling) wraps precompile execution in a broad try/catch that safely converts this into a normal EVM revert, or whether such an exception could propagate and disrupt block processing. This is the critical open question that determines actual severity (contract-level revert vs. node-level fault).

### Likelihood Explanation
Any account can invoke these precompiles via a `TriggerSmartContract` call to the fixed precompile addresses (batch-validate-sign / validate-multi-sign), fully controlling the calldata that becomes `data` and the `words` array, so the offsets are trivially attacker-controlled.

### Recommendation
Add explicit bounds checks in `extractBytes`, `extractSigArray`, and `extractBytesArray` verifying `0 <= offset` and `offset + len <= data.length` before calling `Arrays.copyOfRange`, returning an empty/failed result instead of throwing, consistent with the defensive style already used in `ByteUtil.parseBytes`. [3](#0-2) 

### Proof of Concept
Not fully constructible from static analysis alone — a working PoC requires confirming (1) the exact TVM dispatch path/addresses for `extractSigArray`/`extractBytesArray` callers and (2) whether exceptions thrown here are caught by an enclosing precompile-invocation handler. I could not locate and inspect that call site (e.g., the `BatchValidateSign`/`ValidateMultiSign` precompile classes' `execute()` bodies) within the remaining tool budget, so I cannot confirm whether this results in a safe revert or an unhandled fault. This uncertainty should be resolved before treating this as a confirmed high-severity finding.

### Citations

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

**File:** common/src/main/java/org/tron/common/utils/ByteUtil.java (L344-353)
```java
  public static byte[] parseBytes(byte[] input, int offset, int len) {

    if (offset >= input.length || len == 0) {
      return EMPTY_BYTE_ARRAY;
    }

    byte[] bytes = new byte[len];
    System.arraycopy(input, offset, bytes, 0, min(input.length - offset, len, true));
    return bytes;
  }
```
