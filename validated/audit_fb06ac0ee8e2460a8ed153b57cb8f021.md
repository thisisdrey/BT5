### Title
Unbounded array allocation from unvalidated ABI array-length field in TVM precompiled-contract helpers - ([File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java])

### Summary
The `extractBytes32Array`, `extractBytesArray`, and `extractSigArray` helper methods used by java-tron's TVM precompiled contracts (the signature/multi-sig verification precompiles reachable via the `CALL`/`STATICCALL` opcodes to a precompile address) read an array-length word directly from attacker-controlled call data and immediately allocate a Java array of that size — before any check ties the declared length to the actual size of the supplied call data. [1](#0-0) [2](#0-1) [3](#0-2) 

### Finding Description
Each of these three helper methods reads `len` from `words[offset].intValueSafe()`, a 256-bit `DataWord` fully controlled by the caller's contract call data, and then performs `new byte[len][]` (or `new byte[len][32]`-equivalent) without validating `len` against the actual remaining length of `data`:

```java
private static byte[][] extractBytes32Array(DataWord[] words, int offset) {
  int len = words[offset].intValueSafe();
  byte[][] bytes32Array = new byte[len][];
  ...
``` [1](#0-0) 

```java
private static byte[][] extractSigArray(DataWord[] words, int offset, byte[] data) {
  if (offset > words.length - 1) {
    return new byte[0][];
  }
  int len = words[offset].intValueSafe();
  byte[][] bytesArray = new byte[len][];
  ...
``` [3](#0-2) 

The only guard present (`offset > words.length - 1`) validates the *offset* into the word array, not the declared *count* itself. `intValueSafe()` clamps a huge `DataWord` down to a large positive `int` (up to `Integer.MAX_VALUE`), so an attacker can encode a length field of e.g. `0x7fffffff` while supplying a tiny call-data buffer. The allocation `new byte[len][]` (an array of ~2^31 object references, and subsequently per-element `byte[32]`/`byte[65]` sub-arrays as the loop runs) happens unconditionally before any `ArrayIndexOutOfBoundsException` from the subsequent `data` access would be thrown. This mirrors the reported Erigon bug class exactly: a length field parsed from untrusted input is used to allocate memory before the corresponding payload size is validated.

This is directly analogous to the ETH/66 `hashCount`/`make([]byte, 32*hashCount)` pattern in the report, except the trigger here is a normal, single signed transaction invoking a TVM precompiled contract (e.g., the multi-sig validation precompile referenced by `ValidateMultiSignContractTest.java`) rather than a p2p protocol message — i.e., it is reachable by any unprivileged contract caller, not a malicious peer, and thus within the analog scope allowed ("TVM opcodes, precompiles and energy metering"). [2](#0-1) 

### Impact Explanation
Because TVM execution is part of deterministic block/transaction application, every full node (including validators) that processes a block containing such a transaction executes the same allocation path in `Manager`'s block-application/transaction-execution flow. A single crafted transaction calling the affected precompile with a spoofed huge array-length field can force a large, unvalidated allocation on every node that validates or re-executes that transaction/block, which can degrade performance or crash the JVM process (OutOfMemoryError) network-wide — a node crash/halt impact, which is in the accepted impact list.

### Likelihood Explanation
Likelihood is Medium-High: exploitation requires only deploying/calling a contract that invokes the vulnerable precompile with crafted call data containing an inflated length word — no special privileges, keys, or peer/network position are needed, only ability to submit an ordinary signed transaction (well within the "unprivileged transaction broadcaster / contract deployer" threat model).

### Recommendation
Before allocating `new byte[len][...]` in `extractBytes32Array`, `extractBytesArray`, and `extractSigArray`, validate that `len` is non-negative and consistent with the amount of data actually available (e.g., `len <= (data.length - offsetInBytes) / elementSize`), rejecting/erroring out (returning a failure result) instead of allocating when the declared count exceeds what the supplied call data can possibly contain. This mirrors the fix pattern in the report: validate the count against the real payload size before any allocation.

### Proof of Concept
1. Deploy or call an existing contract that invokes the TVM precompiled contract using these helpers (e.g., the multi-signature validation precompile referenced in `framework/src/test/java/org/tron/common/runtime/vm/ValidateMultiSignContractTest.java`).
2. Craft the call data so the ABI-encoded "array length" word at the expected offset is a very large value (e.g., `0x7fffffff`) while the remainder of the call data buffer is minimal/short.
3. Submit as a normal signed transaction; when the TVM interpreter dispatches the call to the precompile, `extractSigArray`/`extractBytesArray`/`extractBytes32Array` executes `new byte[len][...]` with the attacker-supplied `len` before any bounds check against actual data size, triggering a large/failing allocation on every node executing the transaction.

Note: I was unable to locate, within the available index, the exact `execute()` method of the specific precompiled contract class that calls `extractSigArray`/`extractBytesArray` (only the helper definitions and a related test file `ValidateMultiSignContractTest.java` were found), so the precise entry-point precompile name/address could not be fully confirmed from the indexed content; a full-repository session would be needed to pinpoint that call site with certainty.

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
