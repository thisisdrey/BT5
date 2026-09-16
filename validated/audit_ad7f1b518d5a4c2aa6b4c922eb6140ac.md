### Title
Unbounded array allocation from attacker-controlled length word in TVM precompile ABI array parsing enables node memory-exhaustion DoS - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
The CVE-2018-20406 bug class is "an attacker-controlled length value that is used to resize/allocate a buffer without validating it against the actual amount of available data, leading to memory exhaustion." The analogous pattern in java-tron is the private helper methods `extractBytes32Array`, `extractBytesArray`, and `extractSigArray` in `PrecompiledContracts.java`, which are used by TVM precompiled contracts (batch signature validation / multi-sign related precompiles) that are directly reachable by any contract call from an unprivileged transaction sender.

### Finding Description
`extractBytes32Array` reads a length word straight from the calldata `words` array and converts it with `intValueSafe()`: [1](#0-0) 

`intValueSafe()` only clamps the value when it does not fit in 4 bytes or decodes negative; any positive value that fits in an `int` (up to `Integer.MAX_VALUE`, i.e. ~2.1 billion) is returned unchanged: [2](#0-1) 

`extractBytes32Array` then immediately does `byte[][] bytes32Array = new byte[len][];` with no check that `len` is consistent with the actual size of `words` (i.e., with the actual calldata length supplied by the caller). The same unchecked pattern repeats in `extractBytesArray` and `extractSigArray`, both of which also derive `len` via `intValueSafe()` before allocating `byte[len][]`: [3](#0-2) 

Because `words` is derived from the TVM call's `data` parameter (fully attacker-controlled ABI-encoded input to a precompile invoked via a `CALL`/`STATICCALL` opcode from a deployed contract), an attacker can craft calldata whose "array length" header word decodes to a huge but valid positive `int` (e.g. `0x7FFFFFFF`), while the actual payload is only a few bytes long. This causes the JVM to attempt to allocate an object-reference array of ~2^31 entries (multiple GB on a 64-bit JVM before OOM), consuming the node's heap and provoking `OutOfMemoryError` while executing a single transaction.

This mirrors CVE-2018-20406 exactly at the bug-class level: a length value taken from untrusted serialized input is used to size a memory allocation without validating it against the real amount of data present, producing a memory-exhaustion condition from a single malicious input.

### Impact Explanation
A single, cheaply-crafted transaction that invokes the vulnerable precompile can trigger an attempted multi-GB allocation on every full node that executes/re-executes the transaction (including during block validation by every SR/witness and full node syncing the chain). This can throw `OutOfMemoryError` inside the JVM, which — depending on where it is caught — can destabilize or crash the node process, or degrade the node under GC pressure, producing a denial-of-service against all nodes on the network as they process/replay this block, since it's not merely a local/energy-metered TVM execution but a low-level array allocation before any energy-based limit could stop it.

### Likelihood Explanation
The precompile is invoked via a normal contract call, meaning any account can deploy or call a contract that constructs and submits calldata reaching this code. The only requirement is knowledge of the precompile's calldata layout (offsets are protocol-fixed) and setting one length word to a large value, which is a trivial, cheap construction (no elevated privileges needed). This is directly reachable by any unprivileged transaction broadcaster.

### Recommendation
Bound-check every `len` derived from an ABI array-length word against the actual size of `words`/`data` before allocating (`len <= (words.length - offset - 1)` or equivalent bytes-available check) in `extractBytes32Array`, `extractBytesArray`, and `extractSigArray`, rejecting the precompile call (returning `Pair.of(false, ...)`) instead of allocating when the declared length exceeds what the supplied calldata can actually contain.

### Proof of Concept
Construct calldata for the precompile that uses `extractBytes32Array`/`extractBytesArray` (e.g. the batch-signature-validation precompile) such that the length word at the expected array-length offset is set to `0x7FFFFFFF` while the surrounding calldata otherwise satisfies any preceding format checks; submit this via a contract call (`TriggerSmartContract`) invoking the precompile address. On execution, `new byte[0x7FFFFFFF][]` is attempted, which will throw `OutOfMemoryError` on typical JVM heap configurations, verifiable by tracing execution into `PrecompiledContracts.java` lines 390-397 / 399-412 with the crafted `data`.

I was not able to fully trace every call site that invokes `extractBytes32Array`/`extractBytesArray`/`extractSigArray` (they are only referenced within `PrecompiledContracts.java`, but the exact precompile classes calling them and whether any additional upstream length validation exists before the call were not fully confirmed in the available index). This should be verified against the full file before treating the PoC as final.

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
