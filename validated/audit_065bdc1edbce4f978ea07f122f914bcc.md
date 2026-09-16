### Title
Unbounded array allocation in `BatchValidateSign`/`ValidateMultiSign` precompiles allows attacker-triggered OutOfMemoryError - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The CVE describes a bug class where a length field taken from external/attacker-controlled input is used to size a memory allocation with no practical upper bound, allowing memory exhaustion. The `extractBytes32Array` helper in `PrecompiledContracts.java` has the same pattern: it reads an array-length word straight from calldata and immediately allocates a two-dimensional array of that size with no bound check.

### Finding Description
`extractBytes32Array` derives `len` directly from attacker-supplied `words[offset].intValueSafe()` and allocates `new byte[len][]` unconditionally: [1](#0-0) 

This is invoked from `BatchValidateSign.doExecute` to build the `addresses` array: [2](#0-1) 

The only guard on the size (`sigArraySize`/`addrArraySize > MAX_SIZE`) is gated behind the `VMConfig.allowTvmSelfdestructRestriction()` feature flag; the actual `extractBytes32Array` call for `addresses` is unconditional and unbounded whether or not that flag is on. Similarly, `extractBytesArray` (used for the sibling `signatures` extraction and by `ValidateMultiSign`) allocates `new byte[len][]` from an unchecked `len` before any size validation: [3](#0-2) 

Because `len` comes from a 256-bit `DataWord` truncated via `intValueSafe()`, an attacker can craft calldata whose length-encoding word decodes to a very large positive `int` (up to `Integer.MAX_VALUE`), causing the JVM to attempt an allocation of an array with that many `byte[]` references (8 bytes/reference on 64-bit JVMs, i.e. up to ~16 GB) before any subsequent bounds/energy check can reject the call. This precompile is reachable by any account issuing a `CALL`/`STATICCALL` to precompiled address `0x9` (`BatchValidateSign`) or `0xa` (`ValidateMultiSign`) from a smart contract, i.e. by an unprivileged, ordinary user-submitted transaction.

### Impact Explanation
A single crafted transaction that reaches `PrecompiledContracts.BatchValidateSign#execute` (or `ValidateMultiSign#execute`) with a malicious offset/length word can force the node executing the transaction to attempt a multi-gigabyte array allocation, throwing `OutOfMemoryError` on the JVM. Because block validation on every full node executes the same transaction during block application in `Manager`, a single transaction embedded in a block could cause the same crash across all/most full nodes processing that block, which is a node crash / potential chain-halt condition rather than a mere resource-only slowdown, matching the "node crash or halt" acceptance criterion.

### Likelihood Explanation
Reaching this code path requires only deploying/calling a contract that performs a low-level `CALL` to the fixed precompile addresses with crafted calldata — no special privilege, no SR/witness/committee role, and no p2p manipulation is needed. The `getEnergyForData` cost model for these precompiles is based on `data.length / WORD_SIZE`, i.e. on the calldata size itself, not on the value encoded in the length word, so an attacker can pay a small, fixed energy cost while encoding an oversized `len` value in a single 32-byte word, decoupling the cost paid from the memory that gets requested.

### Recommendation
Add an explicit upper bound check on `len` in `extractBytes32Array`, `extractBytesArray`, and `extractSigArray` immediately after `words[offset].intValueSafe()` is read, before allocating any array, unconditionally (not gated by `allowTvmSelfdestructRestriction`). Reject with `Pair.of(false, EMPTY_BYTE_ARRAY)` (or the `DATA_FALSE` sentinel already used elsewhere) whenever `len` exceeds the precompile's `MAX_SIZE` constant (5 for `ValidateMultiSign`, 16 for `BatchValidateSign`) or a sane absolute ceiling, matching the intent already expressed by the existing (but incompletely applied) `sigArraySize/addrArraySize > MAX_SIZE` checks.

### Proof of Concept
Conceptually: construct calldata for `batchvalidatesign(bytes32,bytes[],address[])` such that the `address[]` offset word points to a length slot encoding a very large 32-bit value (e.g., `0x7fffffff`), while keeping total calldata small (a handful of words) so `getEnergyForData` charges minimal energy. Calling this from a contract via `CALL` to precompiled address `0x9` triggers `extractBytes32Array(words, offset)`, which executes `new byte[0x7fffffff][]`, attempting to allocate roughly 16 GB of reference storage and driving the executing JVM toward `OutOfMemoryError`.

**Uncertainty:** I could not fully verify the exact clamp behavior of `DataWord.intValueSafe()` (i.e., whether it clamps to `Integer.MAX_VALUE` or throws for out-of-int-range values) because the index did not return that method's body for `DataWord.java`; this should be confirmed in a live session before finalizing severity, since it determines the exact maximum `len` an attacker can encode without triggering an earlier exception.

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1165-1177)
```java
      if (VMConfig.allowTvmSelfdestructRestriction()) {
        int sigArraySize = words[words[1].intValueSafe() / WORD_SIZE].intValueSafe();
        int addrArraySize = words[words[2].intValueSafe() / WORD_SIZE].intValueSafe();
        if (sigArraySize > MAX_SIZE || addrArraySize > MAX_SIZE) {
          return Pair.of(true, DATA_FALSE);
        }
      }

      byte[][] signatures = VMConfig.allowTvmSelfdestructRestriction() ?
          extractSigArray(words, words[1].intValueSafe() / WORD_SIZE, data) :
          extractBytesArray(words, words[1].intValueSafe() / WORD_SIZE, data);
      byte[][] addresses = extractBytes32Array(
          words, words[2].intValueSafe() / WORD_SIZE);
```
