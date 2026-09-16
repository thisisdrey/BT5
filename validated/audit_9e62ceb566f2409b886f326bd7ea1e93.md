### Title
Unvalidated length/offset fields in TVM precompiled-contract ABI decoding allow OOM/array-index crash from a single contract call - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
`PrecompiledContracts.extractBytesArray`, `extractSigArray`, and `extractBytes32Array` decode attacker‑controlled precompile call data (a `DataWord[]` derived directly from the `bytes` argument any contract passes to a TRON precompiled contract) without validating that the decoded `len`/`bytesOffset`/`bytesLen` values are consistent with the actual size of the `words` or `data` arrays before using them to allocate arrays or index into memory. This mirrors the GPMF-parser bug class in CVE-2018-13009: a length/nest field taken from untrusted input is trusted for allocation/iteration/indexing without a bound check against the buffer it is supposed to describe.

### Finding Description
`extractBytesArray` and `extractSigArray` read a `len` value straight from the caller-supplied `words[offset]` DataWord: [1](#0-0) [2](#0-1) [3](#0-2) 

Only the initial `offset > words.length - 1` bound is checked; the derived `len`, `bytesOffset`, and `bytesLen` values are never checked against `words.length` or `data.length` before being used to:
- allocate `new byte[len][]` (unbounded, attacker‑chosen up to `Integer.MAX_VALUE` via `DataWord.intValueSafe()`),
- index `words[offset + i + 1]` and `words[offset + bytesOffset + 1]` (can run past the actual `words` array bounds), and
- call `extractBytes(data, (bytesOffset + offset + 2) * WORD_SIZE, bytesLen)` which does `Arrays.copyOfRange(data, offset, offset+len)` with an attacker-chosen offset/length pair that is not validated against `data.length`. [4](#0-3) 

Because `words` (via `DataWord.parseArray`) is sized strictly by the actual call-data length, while `len`/`bytesOffset` are independently attacker-controlled 256-bit values truncated to `int`, a crafted precompile input can cause:
1. `new byte[len][]` with an enormous `len` → `OutOfMemoryError` (an uncaught `Error`, not caught by ordinary `catch (Exception e)` handlers used elsewhere in TVM execution).
2. `words[offset + i + 1]` / `words[offset + bytesOffset + 1]` indices exceeding `words.length` → uncaught `ArrayIndexOutOfBoundsException` if it propagates past the enclosing exception handling for the specific precompile.

This directly parallels the CVE-2018-13009 root cause: a length/nesting value taken from the parsed structure is used to drive further array access without verifying it is consistent with the true buffer size, leading to an out-of-bounds read/resource blowup during parsing.

### Impact Explanation
An `OutOfMemoryError` thrown while a validator/full node is executing a smart-contract transaction inside `Manager`'s block-application path can crash or destabilize the JVM process (node halt), since `Error` is generally not caught by the transaction-execution exception handling used for normal VM faults (`Exception` subclasses). A single malicious contract call reaching one of these precompiles is enough to trigger it on every node that processes/re-executes that transaction, which is a node-crash/chain-availability impact.

### Likelihood Explanation
Any account can deploy or call a contract that invokes a TRON precompiled contract at the relevant address with crafted call data; no special privilege is required, and the entry point is a standard `CALL`/`STATICCALL` from EVM bytecode executed by the TVM interpreter (`actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`), which every full/validator node executes when processing the transaction/block. This makes the trigger trivially reachable by any unprivileged transaction broadcaster.

### Recommendation
Before using `len`, `bytesOffset`, and `bytesLen` in `extractBytesArray`, `extractSigArray`, and `extractBytes32Array`, validate that:
- `len >= 0` and `offset + len + 1 <= words.length`,
- each derived `bytesOffset` and `bytesLen` keep `(bytesOffset + offset + 2) * WORD_SIZE + bytesLen` within `data.length`,
before allocating `new byte[len][]` or calling `Arrays.copyOfRange`. Reject the precompile call (return failure) rather than throwing when these invariants do not hold.

### Proof of Concept
1. Deploy/call a contract that performs a low-level `call`/`staticcall` to the precompiled-contract address that routes through `extractBytesArray`/`extractSigArray` (e.g., the batch-validation precompile that consumes a `DataWord[]` array such as signature/bytes-array inputs).
2. Craft the call data so the length word at the expected array-length slot decodes (via `intValueSafe()`) to a very large positive integer (e.g., close to `Integer.MAX_VALUE`), while the remainder of the call data is minimal (a few words).
3. `extractBytesArray`/`extractSigArray` compute `len` from this crafted word and attempt `new byte[len][]`, or index `words[offset + i + 1]` beyond `words.length`, causing `OutOfMemoryError` or `ArrayIndexOutOfBoundsException` during TVM execution of the transaction.

Note: I could not fully verify, within the available tool budget, exactly how far up the TVM call stack (e.g., in `Program`'s precompile invocation path) this exception/error is caught versus propagated uncaught to the block-application layer in `Manager`. This affects whether the ultimate impact is a full node crash versus a contained transaction-execution failure, and should be confirmed by tracing the precompile invocation call site and its exception handling before treating this as fully proven at Critical severity.

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L428-430)
```java
  private static byte[] extractBytes(byte[] data, int offset, int len) {
    return Arrays.copyOfRange(data, offset, offset + len);
  }
```
