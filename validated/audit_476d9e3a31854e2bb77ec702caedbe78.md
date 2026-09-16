## Finding: Unbounded array allocation from attacker-controlled length field in TVM precompiled-contract argument parsers

### Title
Unbounded `byte[len][]` allocation from unchecked call-data length in `PrecompiledContracts` array parsers - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
`PrecompiledContracts.extractBytes32Array`, `extractBytesArray`, and `extractSigArray` read an array-length field directly out of the precompile's call data and use it, unvalidated, to allocate a Java array with `new byte[len][]`. There is no upper bound check on `len` before allocation, matching the CVE-2019-7698 bug class (an attacker-controlled size field is used directly in a capacity/allocation call, with no sanity check, causing an attempt at excessive memory allocation).

### Finding Description [1](#0-0) 
`extractBytes32Array` takes `len = words[offset].intValueSafe()` from decoded call-data words and immediately does `new byte[len][]` with no ceiling check. [2](#0-1) 
`extractBytesArray` performs the same unchecked pattern: `len = words[offset].intValueSafe(); byte[][] bytesArray = new byte[len][];`. [3](#0-2) 
`extractSigArray` (used for signature-array parsing, given the `SIG_LENGTH` constant, in the batch-signature-verification precompile) repeats the same pattern: `len = words[offset].intValueSafe(); byte[][] bytesArray = new byte[len][];` before any per-item bound is enforced.

`intValueSafe()` is a `DataWord` helper that clamps very large 256-bit values into an `int` range rather than rejecting the value outright, so an attacker can set the length word to a value close to `Integer.MAX_VALUE`. None of these three helper methods enforce an upper bound on `len` relative to the actual `data`/`words` array size before allocating — the allocation happens before any per-element bounds validation occurs (the bounds/array-access errors, e.g. `ArrayIndexOutOfBoundsException`, would only occur later, inside the loop, when reading from `words[offset + i + 1]`).

This is structurally the same root-cause pattern as CVE-2019-7698 in Bento4's `AP4_Array<AP4_CttsTableEntry>::EnsureCapacity`: a size value taken from untrusted input is used directly to size a memory allocation, with no sanity ceiling, so crafted input can trigger an attempt at excessive memory allocation.

### Impact Explanation
Any account can call a smart contract that performs a `CALL`/`STATICCALL` to the precompiled contract address backed by these array-parsing helpers, supplying call data whose length word is set to a very large value (e.g. `0x7fffffff`). This directly forces the JVM to attempt to allocate a `byte[Integer.MAX_VALUE][]` array (an array of ~2 billion object references, i.e. multiple gigabytes just for the reference slots before any element is populated). This throws a `java.lang.OutOfMemoryError`, which is a JVM `Error`, not an `Exception`. If the surrounding TVM execution/precompile dispatch path only catches `Exception` (as is the common pattern elsewhere in this codebase, e.g. broad `catch (Exception e)` handling seen around servlet/precompile boundaries), the `Error` propagates uncaught out of the executing thread, which can crash or destabilize the node process executing the transaction — a full node halt/crash from a single unprivileged transaction is a High/Medium-severity availability impact matching the "node crash or halt" acceptance criterion.

### Likelihood Explanation
Reaching this code only requires broadcasting a normal `TriggerSmartContractContract` (or deploying a contract that performs such a call) that targets the specific precompiled-contract address handled by `extractBytes32Array`/`extractBytesArray`/`extractSigArray`, with a crafted length word in the call data. No special privileges (SR/witness/committee) are needed — this is reachable by any anonymous contract caller, matching the "unprivileged transaction broadcaster / contract deployer" reachability requirement in scope.

### Recommendation
Add an explicit upper-bound check on `len` in `extractBytes32Array`, `extractBytesArray`, and `extractSigArray` (e.g., bound it by the number of remaining `words` available, or by a fixed sane maximum consistent with the precompile's expected input size) before performing `new byte[len][]`, returning a failure/false result instead of allocating when the value is out of range — mirroring the bound checks already present elsewhere in this file (e.g., `VerifyTransferProof`'s `spendCount`/`receiveCount` bound checks at PrecompiledContracts.java lines 1496-1499, and the `ModExp` precompile's `UPPER_BOUND` check).

### Proof of Concept
1. Identify the precompiled-contract address whose `execute()` calls `extractBytes32Array`/`extractBytesArray`/`extractSigArray` (the signature-array parser guarded by `SIG_LENGTH`, consistent with a batch/multi-signature verification precompile).
2. From any account, deploy or reuse a contract that issues a low-level `call` to that precompiled address, or call it directly if it's directly callable via TVM `CALL`.
3. Craft the call data so the length word read at the relevant `words[offset]` position decodes (via `intValueSafe()`) to a very large positive integer (e.g., close to `Integer.MAX_VALUE`).
4. Submit the transaction. The precompile's `execute()` invokes the vulnerable helper, which attempts `new byte[len][]`, throwing `OutOfMemoryError` inside TVM execution. Observe whether this `Error` escapes the precompile/TVM dispatch's exception handling and destabilizes the node process (root-cause verification requires runtime testing, which was not performed as part of this analysis; the exact calling precompile and its exception-handling wrapper should be manually confirmed and exercised in a Devin session with full JVM execution/debug access).

**Note on verification limits:** I was not able to trace the exact precompiled-contract dispatch entry (`getContractForAddress`) that maps to `extractSigArray`/`extractBytesArray`/`extractBytes32Array`, nor confirm whether an enclosing `catch(Throwable)` exists somewhere higher in the TVM call stack that would already absorb the `OutOfMemoryError`, within the available tool budget. This should be verified with a live Devin session (full codebase access, ability to run tests) before treating this as fully confirmed.

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
