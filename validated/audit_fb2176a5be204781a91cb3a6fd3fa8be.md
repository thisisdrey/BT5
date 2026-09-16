Based on my investigation, I found a strong analog to the SES `desc_ptr` bounds-checking bug in the TVM precompiled contract data-extraction helpers.

### Title
Unvalidated length/offset in `PrecompiledContracts` array-extraction helpers can throw `ArrayIndexOutOfBoundsException` from attacker-controlled contract call data - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
`extractBytes32Array`, `extractBytesArray`, and `extractSigArray` in `PrecompiledContracts.java` read a count/offset from attacker-supplied `DataWord[] words` (parsed from a precompiled-contract call's `data`) and use it directly to index into `words` and `data` without validating that the derived index stays within array bounds, mirroring the SES `desc_ptr` pattern where a length/index taken from device-controlled data was used to index a buffer without sanitization.

### Finding Description
`extractBytes32Array` computes `len = words[offset].intValueSafe()` and then loops `i` from `0` to `len`, dereferencing `words[offset + i + 1]` with no check that `offset + len` is within `words.length`: [1](#0-0) 

`extractBytesArray` and `extractSigArray` add a check only for the initial `offset` (`offset > words.length - 1`), but not for the derived `bytesOffset` or the loop bound `len`, so `words[offset + bytesOffset + 1]` and `words[offset + i + 1]` can still index past the end of `words`: [2](#0-1) 

`extractBytes` then does `Arrays.copyOfRange(data, offset, offset + len)` with an `offset`/`len` derived from these unvalidated values, which can also throw `ArrayIndexOutOfBoundsException`/`NegativeArraySizeException` on malformed input: [3](#0-2) 

This is directly analogous to the kernel bug class: a length/index value taken from untrusted input is used to index into a buffer/array without sanitizing it against the buffer's actual bounds, leading to out-of-bounds access.

### Impact Explanation
An unhandled `ArrayIndexOutOfBoundsException`/`NegativeArraySizeException` thrown deep inside TVM precompiled-contract execution (reached via a normal signed smart-contract call to a precompile such as `ValidateMultiSign`/`BatchValidateSign`, which rely on these helpers per `framework/src/test/java/org/tron/common/runtime/vm/ValidateMultiSignContractTest.java` and `BatchValidateSignContractTest.java`) risks crashing the VM execution thread if the exception is not caught by a generic top-level handler. If it propagates uncaught during block application, it can cause a node to halt/crash while processing a block, which is a liveness/availability impact for the node processing the transaction. I could not fully verify within the available context whether a catch-all wraps every precompile `execute()` call in `Program`/VM dispatch, so the exact severity (uncaught crash vs. caught-and-reverted) is not fully confirmed from the index alone.

### Likelihood Explanation
Reachability is straightforward: any unprivileged account can deploy a contract calling a precompile that uses these helpers (`ValidateMultiSign` / `BatchValidateSign`) and craft the ABI-encoded input so the length words point beyond the actual `words`/`data` array bounds, since only a partial bound check (`offset > words.length - 1`) exists and is trivially bypassed by controlling `len`/`bytesOffset` values themselves.

### Recommendation
Add explicit bounds checks for every derived index before array access in `extractBytes32Array`, `extractBytesArray`, and `extractSigArray` — validating `offset + len`, `offset + bytesOffset + 1`, and the final `data` slice bounds against `words.length` and `data.length` respectively — and return an empty/failure result instead of allowing an exception to propagate, consistent with the existing partial check pattern already present for `offset`.

### Proof of Concept
Not able to fully construct a concrete PoC transaction/bytecode from the index alone (would require confirming the exact TVM opcode dispatch/precompile invocation path and its exception handling in `Program`, which is outside what the index makes available); a background Devin session with full repo and test-execution access would be needed to confirm exploitability end-to-end (e.g., by extending `ValidateMultiSignContractTest`/`BatchValidateSignContractTest` with a crafted `data` payload whose embedded length words exceed the `words` array bounds and observing whether an uncaught exception propagates out of contract execution).

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L428-430)
```java
  private static byte[] extractBytes(byte[] data, int offset, int len) {
    return Arrays.copyOfRange(data, offset, offset + len);
  }
```
