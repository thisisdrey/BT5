### Title
Heap-buffer-overflow-class OOB array read/allocation in `extractBytesArray`/`extractBytes32Array` reachable via `ValidateMultiSign`/`BatchValidateSign` precompiles - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
The CVE-2026-22255 bug class is an unchecked, attacker-controlled length used to size/index a buffer in `CIccCLUT::Init()`, producing a heap-buffer-overflow. The analogous pattern in java-tron is the unvalidated, attacker-controlled length words used to allocate and index `byte[][]` arrays in `PrecompiledContracts.extractBytesArray` / `extractBytes32Array`, which are reached from the `ValidateMultiSign` and `BatchValidateSign` precompiled contracts, both callable by any contract via `STATICCALL`/`CALL` from an unprivileged transaction.

### Finding Description
`extractBytes32Array` reads a length word directly from calldata-derived `DataWord[] words` and uses it, unguarded, both to allocate an array and to index further into `words`: [1](#0-0) 

Unlike `extractBytesArray`, which at least checks `offset > words.length - 1` before reading `words[offset]`, `extractBytes32Array` performs no such bound check before dereferencing `words[offset]`, and the subsequent loop `words[offset + i + 1]` can run past the end of the `words` array (an `ArrayIndexOutOfBoundsException`), or, if `len` is attacker-controlled and large, attempt to allocate an enormous `byte[len][]` (`new byte[len][]` at line 392), causing OOM/DoS.

`extractBytesArray` itself also computes offsets and lengths from attacker-supplied words without validating `bytesOffset`/`bytesLen` against `data.length` before calling `extractBytes`, which does an unchecked `Arrays.copyOfRange`: [2](#0-1) [3](#0-2) 

These helpers are invoked from `ValidateMultiSign.execute()` and `BatchValidateSign.doExecute()`, both of which are precompiled contracts reachable from any TVM contract call (address range gated by `VMConfig.allowTvmSelfdestructRestriction()`/TIP-854 checks that only apply post-activation and only bound `sigArraySize` via `MAX_SIZE`, not the raw `len` fed into `extractBytes32Array`'s address-array path before that check runs): [4](#0-3) 

Specifically, in `BatchValidateSign.doExecute`, `addresses = extractBytes32Array(words, words[2].intValueSafe() / WORD_SIZE)` is called unconditionally, with no upper bound on the length word it reads at `words[offset]`, before the `cnt > MAX_SIZE` check on line 1179 is ever evaluated. A crafted `data` payload can set `words[2]` to point at an offset whose value (`len`) is a huge integer (up to `Integer.MAX_VALUE`), directly triggering the unguarded `new byte[len][]` allocation or an out-of-bounds array index inside `words`.

### Impact Explanation
An attacker who calls `BatchValidateSign` (address for `batchvalidatesign(bytes32,bytes[],address[])`) or `ValidateMultiSign` with crafted calldata can:
- Trigger `ArrayIndexOutOfBoundsException` deep inside precompile execution, which is only partially caught (`BatchValidateSign.execute` wraps `doExecute` in a `try/catch(Throwable)` that swallows most exceptions and returns a zeroed result — masking the bug rather than preventing resource exhaustion), or
- Trigger an OOM-inducing huge array allocation (`new byte[len][]`) that can crash or stall the node process executing the transaction, since TVM/precompile execution runs inside the shared node process handling all transactions.

This matches the "node crash or halt" acceptance criterion for a Medium/High finding, reachable purely from an unprivileged contract call.

### Likelihood Explanation
Both `ValidateMultiSign` and `BatchValidateSign` are ordinary precompiled contracts invokable by any account via a normal `CALL`/`STATICCALL` from any deployed contract, requiring no special privilege — any contract deployer/anonymous transaction broadcaster can construct calldata with attacker-chosen `DataWord` values for the length/offset fields consumed by `extractBytes32Array`/`extractBytesArray`. The `execute` methods only validate encoding shape (`isValidAbiEncoding`) when `VMConfig.allowTvmOsaka()` is active, and even then this only enforces overall calldata word alignment, not that individual length fields are within safe bounds before being used to size/index arrays.

### Recommendation
- Add explicit bounds validation in `extractBytes32Array` mirroring `extractBytesArray`'s `offset > words.length - 1` guard, and additionally validate that `len` (and `offset + len`) does not exceed `words.length`, rejecting the call (returning `false`/empty) instead of allocating or indexing unchecked.
- Cap `len` before allocation (e.g., against a small `MAX_SIZE`-like constant) prior to the `new byte[len][]` allocation in both `extractBytesArray` and `extractBytes32Array`.
- In `extractBytesArray`, validate `bytesOffset`/`bytesLen` against `data.length` before calling `extractBytes`.
- Avoid catching `Throwable` broadly in `BatchValidateSign.execute` in a way that silently converts genuine bugs (e.g., resource exhaustion mid-loop) into a benign zero result; add tests specifically for oversized/adversarial length words feeding these two helpers.

### Proof of Concept
1. Deploy any contract and call it such that it issues `CALL` (or the equivalent Solidity `batchvalidatesign(bytes32,bytes[],address[])` call) to the `BatchValidateSign` precompiled address with a hand-crafted `data` payload where:
   - `words[0]` = arbitrary hash,
   - `words[1]` = valid offset to a well-formed small `bytes[]` array (to pass the earlier `sigArraySize` check if TIP-854 checks are active),
   - `words[2]` = an offset value that, once divided by `WORD_SIZE`, points to a `words[offset]` slot set to a very large value (e.g., `0x7FFFFFFF`) — this is treated as `len` in `extractBytes32Array`.
2. Invoke `BatchValidateSign.execute(data)` (directly reachable via `doExecute` at `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java:1176`).
3. Observe that `extractBytes32Array` executes `new byte[len][]` with `len` derived directly from attacker data before any `MAX_SIZE` bound is applied to that array, causing an `OutOfMemoryError`/huge-allocation attempt or an `ArrayIndexOutOfBoundsException` on subsequent `words[offset + i + 1]` accesses — reachable with a single crafted contract call, no special privileges required. [1](#0-0) [5](#0-4)

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L428-430)
```java
  private static byte[] extractBytes(byte[] data, int offset, int len) {
    return Arrays.copyOfRange(data, offset, offset + len);
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1160-1179)
```java
        return Pair.of(false, EMPTY_BYTE_ARRAY);
      }
      DataWord[] words = DataWord.parseArray(data);
      byte[] hash = words[0].getData();

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
      int cnt = signatures.length;
      if (cnt == 0 || cnt > MAX_SIZE || signatures.length != addresses.length) {
```
