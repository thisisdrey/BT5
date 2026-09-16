### Title
Unvalidated ABI-encoded array length triggers excessive/attacker-controlled memory allocation in TVM precompiled contracts - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
Several precompiled-contract helper routines in `PrecompiledContracts.java` read an array-length field directly from attacker-supplied ABI-encoded call data and use it to size a Java array **before** validating that the value is consistent with the actual size of the data buffer. This mirrors the CVE-2019-7704 bug class in Binaryen's `readUserSection`, where a length value taken from untrusted input is used to allocate memory without bounding it against the real payload size, allowing an attempt at excessive memory allocation.

### Finding Description
`extractBytes32Array`, `extractBytesArray`, and `extractSigArray` all read a "length" word straight out of the caller-controlled ABI `words` array and immediately allocate an array sized to that value: [1](#0-0) [2](#0-1) [3](#0-2) 

In each of these methods, `int len = words[offset].intValueSafe();` is used directly to allocate `new byte[len][]` (a reference array, so `len` up to `Integer.MAX_VALUE` attempts to allocate on the order of gigabytes of heap for null references alone). The only guard present is `offset > words.length - 1`, which checks that the *offset* is in range — it does **not** check that `len` is consistent with the number of remaining words in the call data. An attacker can therefore craft a 32-byte ABI word with an arbitrary large value (e.g. `0x7fffffff`) at the "array length" position while keeping the rest of the call data tiny, causing the JVM to attempt a huge allocation for `bytes32Array`/`bytesArray`/`bytesArray` prior to any subsequent bounds check inside the following loop (which would otherwise throw `ArrayIndexOutOfBoundsException` on the *next* word access, but only after the oversized array has already been allocated).

This is directly analogous to the reported Binaryen issue: a length value is trusted and used to drive an allocation without first validating it against the bounds of the actual input buffer.

### Impact Explanation
A transaction that invokes the affected precompiled contract(s) with crafted call data can force the executing node to attempt a very large heap allocation. Depending on JVM heap configuration this can:
- Throw `OutOfMemoryError` on the executing thread, potentially destabilizing or crashing the node process (denial of service), and
- Cause excessive GC pressure/latency for all other transactions being processed concurrently by the same node.

Because block/transaction execution in java-tron is deterministic and shared across all full nodes, a transaction that reliably triggers this on one node will trigger it identically on every validating node, giving an unprivileged attacker a way to degrade or crash consensus-critical nodes network-wide — this qualifies as a node crash/halt-class impact.

### Likelihood Explanation
The precompiled-contract execution path is reachable by any account that can submit a `TriggerSmartContract` transaction (or have a deployed contract issue a `CALL`/`STATICCALL`) targeting one of the addresses handled by `PrecompiledContracts.getContractForAddress` whose implementation calls these helpers with attacker-influenced `data`. No special privileges, signatures beyond a normal account key, or contract deployment rights beyond standard TVM usage are required, making likelihood high for any precompile that routes raw call data through `extractBytes32Array`/`extractBytesArray`/`extractSigArray` without pre-validating the length word against `words.length`.

### Recommendation
Before allocating any array sized by an untrusted length field extracted from call data (`extractBytes32Array`, `extractBytesArray`, `extractSigArray`), validate that `len` is non-negative and does not exceed the number of remaining words/bytes actually available in `words`/`data` (e.g. `len <= words.length - offset - 1`), returning an error/empty result otherwise instead of proceeding to allocate. Apply the same defensive bound used elsewhere in the codebase, such as the `verifyLength` pattern in `RLP.java`: [4](#0-3) 

### Proof of Concept
1. Craft a `TriggerSmartContract` transaction calling a precompiled-contract address whose implementation forwards `data` into `extractBytes32Array`/`extractBytesArray`/`extractSigArray`.
2. In the ABI-encoded `data`, place a 32-byte word at the expected "array length" offset with a huge value (e.g. `0x000000000000000000000000000000000000000000000000000000007FFFFFFF`), while making the surrounding data buffer small.
3. Submit the transaction; on execution, the node calls e.g. `extractBytes32Array(words, offset)`, which computes `len = words[offset].intValueSafe()` and executes `new byte[len][]`, attempting an allocation far larger than any legitimate need, exhausting heap and potentially throwing `OutOfMemoryError` on the executing node thread.

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

**File:** framework/src/main/java/org/tron/core/capsule/utils/RLP.java (L612-617)
```java
  private static void verifyLength(int suppliedLength, int availableLength) {
    if (suppliedLength > availableLength) {
      throw new RuntimeException(String.format("Length parsed from RLP (%s bytes) is greater "
          + "than possible size of data (%s bytes)", suppliedLength, availableLength));
    }
  }
```
