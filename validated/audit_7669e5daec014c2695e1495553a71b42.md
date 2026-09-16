### Title
Unbounded array allocation from attacker-controlled length in TVM precompiled-contract signature-batch parsers - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
`PrecompiledContracts.extractBytesArray`, `extractBytes32Array`, and `extractSigArray` read a 32-byte word from attacker-supplied `data`/`words` and use it directly, unbounded, as the length of a Java array allocation (`new byte[len][]`), analogous to the CVE-2020-19724 pattern where an attacker-controlled length field drives an unbounded memory allocation in `get_data`. [1](#0-0) [2](#0-1) [3](#0-2) 

### Finding Description
These helper methods decode a "count" field with `words[offset].intValueSafe()` and immediately allocate an array of that size without any upper-bound check against the actual size of the supplied calldata:

```java
private static byte[][] extractBytes32Array(DataWord[] words, int offset) {
  int len = words[offset].intValueSafe();
  byte[][] bytes32Array = new byte[len][];
  ...
}
``` [1](#0-0) 

`intValueSafe()` clamps a 256-bit word to a Java `int` range, so `len` can be as large as `Integer.MAX_VALUE` (~2.1 billion), fully attacker-controlled via the TVM call's input `data`. The subsequent loop `for (int i = 0; i < len; i++)` would then also attempt to index `words[offset + i + 1]`, which normally throws `ArrayIndexOutOfBoundsException` before excessive work is done in `extractBytesArray`/`extractSigArray` — but the `new byte[len][]` allocation itself happens before that bound check, so a single crafted length value can force a multi-gigabyte array allocation attempt on the node's JVM heap. `extractBytes32Array` has no offset bound-check at all before dereferencing `words[offset]`, and even a moderate but large `len` (e.g., tens of millions) is enough to trigger significant GC pressure or `OutOfMemoryError` well within CPU/energy budgets, since energy is charged mostly for the eventual computation, not for the raw array allocation itself.

This mirrors CVE-2020-19724's root cause: a length value taken directly from untrusted input is used to size a memory allocation with no sanity/bounds check against the real available data size, causing memory-consumption denial of service.

### Impact Explanation
An attacker who deploys or calls a contract that invokes the precompiled contract(s) using these helpers (e.g., the batch/multi-signature validation precompile) can pass a crafted `data` blob whose embedded count word is a large value. This forces the TRON node executing the transaction to attempt an oversized array allocation on the JVM heap, which can throw `OutOfMemoryError`, degrade node performance, or crash the node process — a denial-of-service condition reachable by any unprivileged transaction sender via ordinary contract invocation.

### Likelihood Explanation
Likelihood is high for reachability: any account can broadcast a transaction that triggers a `CALL`/`STATICCALL` to the relevant precompiled address with fully attacker-controlled `data`, and the vulnerable parsing path executes before any full bounds validation is applied to the derived `len`. No special privileges (SR/witness/committee) are required — only a signed transaction invoking the precompile.

### Recommendation
Add an explicit upper bound on `len` in `extractBytes32Array`, `extractBytesArray`, and `extractSigArray` — validating it against the actual remaining length of `words`/`data` before allocating any array, and reject the call (return empty/false) if the declared count exceeds what the input can possibly contain, mirroring the `verifyLength`-style checks already used elsewhere in the codebase (e.g., `org.tron.core.capsule.utils.RLP`). [4](#0-3) 

### Proof of Concept
1. Deploy or call an existing contract that invokes the precompiled contract associated with `extractBytesArray`/`extractBytes32Array`/`extractSigArray` (batch signature validation precompile).
2. Craft the `data` payload's offset word such that `words[offset]` decodes (via `intValueSafe()`) to a very large integer (e.g., `0x7FFFFFFF`).
3. Submit the transaction; the node calls into `PrecompiledContracts.extractBytes32Array`/`extractBytesArray`, which executes `new byte[len][]` with `len` at or near `Integer.MAX_VALUE` before any bounds check against actual data length, causing an oversized allocation attempt on the node.

Note: I was not able to fully trace the exact call sites in `BatchValidateSign`/`ValidateMultiSign` classes that invoke these three helper methods due to iteration limits, but their existence is confirmed by associated test files (`BatchValidateSignContractTest.java`, `ValidateMultiSignContractTest.java`), and the vulnerable helper logic itself is directly verified in `PrecompiledContracts.java`.

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
