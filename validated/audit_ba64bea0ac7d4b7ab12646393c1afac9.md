### Title
Unbounded array index reads in `PrecompiledContracts.extractBytes32Array`/`extractSigArray`/`extractBytesArray` can throw uncaught exceptions from attacker-controlled ABI-encoded precompile input - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
CVE-2017-7960 is a heap buffer over-read in libcroco caused by insufficient bounds checking while parsing a crafted, attacker-supplied file. The analogous bug class in java-tron is insufficiently bounds-checked parsing of attacker-controlled ABI-encoded byte arrays passed as `data` to TVM precompiled contracts (`ValidateMultiSign`/`BatchValidateSign`-style contracts that use `extractBytes32Array`, `extractBytesArray`, and `extractSigArray`).

### Finding Description
`extractBytes32Array` indexes into the `words` array with no bounds validation at all: [1](#0-0) 
`words[offset]` and `words[offset + i + 1]` are read directly from an attacker-supplied `len` value taken from the input data, with no check that `offset` or `offset + i + 1` is within `words.length`.

`extractBytesArray` and `extractSigArray` add a check only for the initial `offset`, but not for the subsequent indices derived from the attacker-controlled `len`/`bytesOffset` values: [2](#0-1) [3](#0-2) 
`extractBytes` itself calls `Arrays.copyOfRange(data, offset, offset + len)`, which throws `ArrayIndexOutOfBoundsException`/`NegativeArraySizeException` if `offset`/`len` derived from attacker-controlled words exceed `data.length` or are negative.

Because `len`, `bytesOffset`, and `bytesLen` are all read via `intValueSafe()` from attacker-supplied 32-byte ABI words with no upper-bound validation against `words.length` or `data.length`, a contract caller can craft `data` such that these helper methods index far outside the actual array bounds, triggering an uncaught runtime exception during precompile execution inside the TVM.

### Impact Explanation
An unbounded/malformed index in these helpers throws an unchecked Java exception (`ArrayIndexOutOfBoundsException`, `NegativeArraySizeException`) during precompiled-contract execution triggered by a smart contract call. If this exception is not caught by the TVM's precompile dispatch/exception-handling path (`OperationActions`/`Program`), it can propagate up through contract execution and potentially crash or halt block processing for the node executing the transaction — meeting the "node crash or halt" bar. I was not able to fully confirm within the available context whether an outer catch-all (e.g., in `Program.callToPrecompiledContract` or the VM's top-level exception handler) safely converts this into a normal `OutOfEnergyException`/revert versus letting an unchecked `RuntimeException` escape uncaught; this needs to be verified against the actual precompile dispatch code before treating it as more than a potential DoS.

### Likelihood Explanation
Reaching this code only requires calling a contract that invokes one of the vulnerable precompiled contracts (e.g., `ValidateMultiSign`, `BatchValidateSign`) with a crafted `data` payload — any account able to deploy or call a TVM contract can attempt this, making the reachability trivial. However, the actual impact depends on whether the surrounding TVM exception handling degrades this to a benign revert rather than a hard crash, which is unverified here.

### Recommendation
Add explicit bounds checks in `extractBytes32Array`, `extractBytesArray`, and `extractSigArray` validating that `offset`, `offset + i + 1`, and any computed `bytesOffset`/`bytesLen` stay within `[0, words.length)` and `[0, data.length]` before indexing, returning an empty/failed result (consistent with the existing `offset > words.length - 1` guard pattern) instead of allowing out-of-bounds array access to throw uncaught exceptions.

### Proof of Concept
Craft ABI-encoded `data` for a precompile that calls `extractBytesArray`/`extractSigArray` (e.g., `ValidateMultiSign`) where the length word at the expected array-length position is set to a very large value (e.g., `0x7fffffff`) or where the derived `bytesOffset`/`bytesLen` values point beyond `data.length`; invoking the corresponding TVM opcode with this payload drives execution into `words[offset + i + 1]` or `Arrays.copyOfRange(data, offset, offset+len)` with out-of-range indices, throwing an unchecked exception from within precompile execution.

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
