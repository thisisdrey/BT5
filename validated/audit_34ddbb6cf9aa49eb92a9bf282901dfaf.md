### Title
Unbounded array length taken directly from TVM precompile call data can crash the node - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
Several precompiled-contract input decoders in `PrecompiledContracts.java` allocate Java arrays whose size is taken directly, and unchecked, from attacker-controlled call data (the `len` value read from a `DataWord`), mirroring the NumPy bug class where an unvalidated "dimension" value drives a native allocation.

### Finding Description
`extractBytes32Array`, `extractBytesArray`, and `extractSigArray` each read an array length straight out of the caller-supplied `data`/`words` and immediately use it to size a new array, with no upper bound or sanity check against the remaining input size: [1](#0-0) [2](#0-1) [3](#0-2) 

`len = words[offset].intValueSafe()` is derived from a 256-bit word fully controlled by the transaction/contract-call payload. The only guard present is `offset > words.length - 1`, which validates that the *offset* is in range — it does not validate that `len` is a sane, small, non-negative value relative to the actual payload size. A crafted `len` can be:
- Very large (close to `Integer.MAX_VALUE`), causing `new byte[len][]` to attempt a massive heap allocation and throw `OutOfMemoryError`, or
- Negative when the raw word's low 32 bits produce a negative `int`, throwing `NegativeArraySizeException`.

Both are unchecked runtime errors during execution of a precompiled contract invoked from the TVM (`CALL`/`STATICCALL` to a precompile address), i.e., reachable from any signed transaction that triggers a contract calling into one of these precompiles (the batch-signature/zk-proof precompiles that consume `DataWord[]`-encoded arrays, as seen in the related `spendCount`/`receiveCount`-driven allocation pattern in the same file): [4](#0-3) 

### Impact Explanation
Because block/transaction execution in java-tron is deterministic and this code runs during TVM contract execution while applying a block, any full node (SR or non-SR) that executes the offending transaction will hit the same uncaught error at the same point. Depending on how `Program`/`Runtime` wraps exceptions from precompile execution, an unchecked `OutOfMemoryError` or `NegativeArraySizeException` that is not converted into a catchable VM exception risks aborting block processing on every node, which is a node-crash / potential-chain-halt class impact rather than a benign revert.

### Likelihood Explanation
Likelihood is uncertain without confirming (a) which precompile(s) call `extractBytesArray`/`extractSigArray`/`extractBytes32Array` and at what precompile address, and (b) whether the surrounding `execute()` method wraps exceptions from these helpers in a catchable `Program.Exception`/`RuntimeException` that gets converted to a revert rather than propagating as an unhandled `Error`. The index/search did not return the exact call sites (`extractBytesArray(...)`, `extractSigArray(...)` invocations) within the time available, so I could not verify the reachable precompile address or confirm whether existing energy/gas metering or generic exception handling around precompile `execute()` already contains this failure mode before it reaches the JVM as an unrecoverable `Error`. This is a material gap — if a broad `catch (Throwable e)` exists around precompile execution (similar to the `catch (Throwable any)` pattern seen elsewhere in the file, e.g., `recoverAddrBySign`), the practical impact could be reduced to a normal revert instead of a crash.

### Recommendation
- Bound `len` explicitly against `0 <= len <= (words.length - offset - 1)` (or a small protocol-defined maximum) before allocating `bytes32Array`, `bytesArray`, or `sigArray` in `extractBytes32Array`, `extractBytesArray`, and `extractSigArray`.
- Reject with a normal `Program.Exception`/precompile failure (returning `false`/empty result) rather than allowing `NegativeArraySizeException` or `OutOfMemoryError` to propagate.
- Audit all precompile `execute()` methods that call these helpers to confirm they are wrapped in exception handling that converts any `Throwable` into a deterministic, catchable VM failure instead of an unhandled `Error`.

### Proof of Concept
Not independently verified end-to-end (the exact precompile entry point and its calldata layout could not be confirmed in the time available). Conceptually: craft calldata for a precompile that internally calls `extractBytesArray`/`extractSigArray` such that the `DataWord` at the expected length offset encodes either `0xFFFFFFFF...` (parses to a large or negative `int` via `intValueSafe()`) or a very large positive 32-bit value (e.g., `0x7FFFFFFF`), then invoke that precompile via a contract `CALL`/`STATICCALL` in a transaction to trigger the oversized/negative array allocation.

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1502-1510)
```java
        byte[][] spendCv = new byte[spendCount][32];
        byte[][] rk = new byte[spendCount][32];
        byte[][] spendProof = new byte[spendCount][192];
        byte[][] spendAuthSig = new byte[spendCount][64];
        byte[][] receiveCm = new byte[receiveCount][32];
        byte[][] receiveCv = new byte[receiveCount][32];
        byte[][] receiveEpk = new byte[receiveCount][32];
        byte[][] receiveProof = new byte[receiveCount][192];

```
