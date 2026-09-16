### Title
Out-of-bounds/malformed-length read in precompiled contract ABI decoding (`extractBytesArray`/`extractBytes`) - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
CVE-2012-6699 describes `decode_search` in dhcpcd trusting an attacker-controlled length field from a network response without validating it against the actual buffer size, causing an out-of-bounds read. The analogous pattern in java-tron is in `PrecompiledContracts.extractBytesArray`/`extractBytes32Array`, which decode length and offset fields directly from attacker-supplied `DataWord[]` call data for a precompiled contract (e.g. the batch-signature-validation precompile) without bounds-checking the derived offsets/lengths against the actual data array size.

### Finding Description
`extractBytesArray` reads an element count from `words[offset].intValueSafe()` and then, for each element, reads `bytesOffset` and `bytesLen` from further words computed purely from attacker input, then calls `extractBytes(data, (bytesOffset + offset + 2) * WORD_SIZE, bytesLen)`: [1](#0-0) 

`extractBytes32Array` similarly trusts `len = words[offset].intValueSafe()` and then indexes `words[offset + i + 1]` for `i` up to `len` without checking that `offset + i + 1` stays within the `words` array bounds: [2](#0-1) 

This mirrors the dhcpcd bug class: a length/offset value taken from untrusted input (here, EVM call data supplied by any contract-calling transaction) is used to drive array indexing/reads without validating it against the real bounds of the backing array (`words` or `data`). A crafted, oversized or negative `len`/`bytesOffset`/`bytesLen` value can push the read index outside the allocated `data`/`words` array, triggering `ArrayIndexOutOfBoundsException` or `NegativeArraySizeException` at runtime.

### Impact Explanation
Since this code executes inside the TVM when any account calls the precompiled contract (this data-extraction helper backs the batch signature validation precompile used from any smart contract), a malicious but otherwise unprivileged caller can craft the input `data`/`words` layout to trigger an unhandled Java exception during precompile execution. Depending on how the caller (`Program.callToPrecompiledAddress`) handles these runtime exceptions, this can manifest as an execution/DoS issue for the transaction, and in the worst case if the exception is not uniformly caught by all nodes (e.g. due to environment/JVM differences) it could cause inconsistent node behavior. This is reachable purely by transaction data.

### Likelihood Explanation
Reachability is high: any account can deploy a contract or craft a transaction that calls the affected precompiled address with attacker-controlled call data, directly exercising `extractBytesArray`/`extractBytes32Array` with no additional privileges required.

### Recommendation
Add explicit bounds validation before using length/offset values derived from call data: verify `len >= 0` and `offset + len + 1 <= words.length` in `extractBytes32Array`, and verify `bytesOffset >= 0`, `bytesLen >= 0`, and that `(bytesOffset + offset + 2) * WORD_SIZE + bytesLen` does not exceed `data.length` in `extractBytesArray`, throwing a well-defined contract-validation/execution exception instead of allowing an unchecked `ArrayIndexOutOfBoundsException`/`NegativeArraySizeException` to propagate.

### Proof of Concept
A transaction calling the precompiled contract that consumes `extractBytesArray`/`extractBytes32Array` with call data where the length word (`words[offset]`) is set to a large positive integer (e.g. `Integer.MAX_VALUE`) or where computed `bytesOffset`/`bytesLen` values point beyond the actual `data` array length would trigger an out-of-bounds array access inside the precompile decoding logic during TVM execution, analogous to a crafted DHCP response length field in the original CVE. [1](#0-0) [2](#0-1)

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
