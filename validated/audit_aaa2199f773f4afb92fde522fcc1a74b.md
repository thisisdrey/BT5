## Analysis

The FreeImage CVE is an "allocate a buffer sized from an untrusted length field, before validating that length against the actual available data" bug. The closest reachable analog in java-tron is in the TVM precompiled-contract signature/array parsing helpers in `PrecompiledContracts.java`, used by the `BatchValidateSign` and `ValidateMultiSign` precompiles (enabled via `VMConfig.allowTvmSolidity059()`), which any contract deployer/caller can invoke through a normal `CALL` opcode to the precompile address.

### Finding Description

`extractBytesArray`, `extractBytes32Array`, and `extractSigArray` all read a 32-byte word from the caller-supplied `data`/`words` array and use it directly as an array-allocation size, with no upper-bound check before the allocation: [1](#0-0) [2](#0-1) [3](#0-2) 

In each case `int len = words[offset].intValueSafe();` is taken from an attacker-controlled 32-byte word embedded in the call data, and immediately used to allocate `new byte[len][]` (an array of object references) — before any relationship between `len` and the actual size of `data`/`words` is validated. Unlike the modular-exponentiation precompile, which explicitly caps `baseLen`/`expLen`/`modLen` to an `UPPER_BOUND` of 1024 before doing any work (as seen in `ModExp.execute`), and unlike the Sapling precompile, which bounds `spendCount`/`receiveCount` to at most 2 before allocating any arrays, these three array-extraction helpers have no equivalent bound. [4](#0-3) 

Because the actual call-data buffer passed into `execute()` can be tiny (e.g. a single word), while the length word inside it can independently be set to a very large value (`intValueSafe()` only prevents `long`/`BigInteger` overflow when reading a `DataWord`, it does not restrict the value to something proportional to `data.length`), a caller can request an allocation of an oversized `byte[][]` (or `byte[]` in `extractBytes32Array`'s inner elements) using a minimal payload — this is exactly the "excessive-size allocation from an untrusted length field" pattern described in CVE-2023-47995 for `FreeImage_AllocateBitmap`.

### Impact Explanation

Allocating a very large object array (`new byte[len][]` with `len` near `Integer.MAX_VALUE`) can trigger an `OutOfMemoryError` inside the node process handling the transaction/contract-call. `OutOfMemoryError` is a JVM `Error`, not an `Exception`; ordinary `catch (Throwable any)` guards used elsewhere in this file (e.g. `ECRecover.execute`) would catch it, but `extractBytesArray`/`extractBytes32Array`/`extractSigArray` themselves are called from the `BatchValidateSign`/`ValidateMultiSign` `execute()` methods with no such broad catch around the allocation itself, so an `OutOfMemoryError` here can propagate and destabilize the full node's transaction-processing/block-application thread — a denial-of-service against block production/serving, matching the "node crash or halt" impact class.

### Likelihood Explanation

Any unprivileged account can trigger this by deploying or calling a contract that issues a `CALL` to the `BatchValidateSign` (`0x...66`) or `ValidateMultiSign` (`0x...67`) precompile address with crafted calldata containing an oversized length word at the relevant offset — no special privilege, large payload, or high fee is required, since the allocation size is decoupled from the transaction's actual data size. This is only reachable when `VMConfig.allowTvmSolidity059()` is active (a chain parameter/fork switch), which should be verified as enabled on the target network.

### Recommendation

Add an explicit upper bound on `len` in `extractBytesArray`, `extractBytes32Array`, and `extractSigArray` (mirroring the `UPPER_BOUND` pattern used in `ModExp` and the `spendCount`/`receiveCount <= 2` pattern used in the Sapling precompile) before allocating the array, and/or validate that `len` is consistent with the actual size of `words`/`data` before allocation.

### Proof of Concept

Construct calldata for a `CALL` to the `BatchValidateSign` or `ValidateMultiSign` precompile address where the 32-byte word at the array-length `offset` (consumed by `extractBytesArray`/`extractSigArray`) is set to a very large value (e.g. `0x7fffffff`), while the remainder of the calldata is minimal. When the precompile's `execute()` reaches `extractBytesArray`/`extractSigArray`/`extractBytes32Array`, it will attempt `new byte[0x7fffffff][]`, an allocation far larger than any bound checked elsewhere in the file, which can throw `OutOfMemoryError` uncaught by the surrounding code paths.

Note: I was unable to directly view the `execute()` bodies of `BatchValidateSign`/`ValidateMultiSign` or the exact `intValueSafe()` clamping bounds in this session (index truncation), so I cannot confirm with 100% certainty whether a broader try/catch exists further up the call stack that would down-grade this to a caught exception instead of a propagating `Error`. If a Devin session is available, verifying `PrecompiledContracts.BatchValidateSign.execute()` / `PrecompiledContracts.ValidateMultiSign.execute()` and `DataWord.intValueSafe()` directly is recommended before treating this as fully confirmed.

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L697-700)
```java
      if (VMConfig.allowTvmOsaka()
          && (baseLen > UPPER_BOUND || expLen > UPPER_BOUND || modLen > UPPER_BOUND)) {
        return Pair.of(false, EMPTY_BYTE_ARRAY);
      }
```
