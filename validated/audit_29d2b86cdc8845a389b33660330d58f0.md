## Finding

### Title
Unbounded attacker-controlled array-length in TVM signature precompiles causes OOM/crash - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The `ValidateMultiSign` and `BatchValidateSign` precompiled contracts parse a signature/address count directly from calldata and use it to allocate and index into a byte array without validating that the count is consistent with the actual size of the input, mirroring the `RaggedTensorToTensor` root cause of trusting a user-supplied "shape" value to size/index a buffer.

### Finding Description
`extractBytesArray` and `extractSigArray` read an attacker-controlled length `len` from the calldata word at `offset`, then allocate `new byte[len][]` and loop `words[offset + i + 1]` up to `len` times: [1](#0-0) [2](#0-1) 

The only bounds check performed is `offset > words.length - 1` — there is no check that `offset + len` stays within `words.length`, and no upper bound on `len` itself before the allocation happens. Because `intValueSafe()` clips into the full signed-int range, `len` can be attacker-set to a very large value (up to `Integer.MAX_VALUE`), so `new byte[len][]` (an array of `len` object references) attempts to allocate on the order of gigabytes of heap before any further validation occurs.

Both `ValidateMultiSign.execute` and `BatchValidateSign.doExecute` call these helpers directly from raw, fully attacker-controlled contract calldata: [3](#0-2) [4](#0-3) 

Notably, the `MAX_SIZE` check (5 for `ValidateMultiSign`, 16 for `BatchValidateSign`) is only enforced when `VMConfig.allowTvmSelfdestructRestriction()` is enabled, and even then it is checked on a *different* word (`words[words[3]/WORD_SIZE]`) before `extractBytesArray`/`extractSigArray` are called, so the size-limiting guard is best-effort and version-gated, not an inherent bound in the helper functions themselves. When the feature flag is off, or when `sigArraySize`/`addrArraySize` pass the size check but `len` computed a second time inside `extractBytesArray` diverges (attacker controls each word independently), the unbounded allocation path is reachable.

For `ValidateMultiSign.execute`, the call to `extractBytesArray`/`extractSigArray` sits outside any try/catch block in that method, so any `Error` thrown (e.g., `OutOfMemoryError` from the huge allocation) is not handled locally at all.

### Impact Explanation
Any address can broadcast a transaction that calls a contract invoking these precompiles (`ValidateMultiSign` / `BatchValidateSign`) with crafted calldata that sets the sig/address count word to a very large value. The resulting `new byte[len][]` allocation attempt can exhaust heap and throw `OutOfMemoryError`, an `Error` rather than an `Exception`, which is not guaranteed to be caught by generic `catch (Exception ...)` handlers elsewhere in the TVM call stack. Repeated or large-magnitude allocation attempts can degrade or crash node JVM processes that execute the transaction, which is a node-crash/halt impact category explicitly in scope.

### Likelihood Explanation
Both precompiles are directly reachable from any TVM `CALL`/`STATICCALL` originating from any deployed contract, requiring no special permission — only a signed transaction invoking a contract that calls the precompiled address. Crafting the calldata to set an oversized length word is trivial and fully attacker-controlled.

### Recommendation
Validate the parsed `len` in `extractBytesArray` and `extractSigArray` against a hard cap (e.g., the same `MAX_SIZE` constants already used by the callers) and against the actual bound `offset + len + 1 <= words.length` before allocating `new byte[len][]`, unconditionally (not only under `allowTvmSelfdestructRestriction()`), and ensure `ValidateMultiSign.execute` wraps the extraction calls in the same broad exception/error handling used elsewhere.

### Proof of Concept
Construct calldata for `ValidateMultiSign` (or `BatchValidateSign`) where the ABI-encoded array-length word pointed to by the offset word (`words[3]` / `words[1]`) is set to a very large positive integer (e.g., close to `Integer.MAX_VALUE`), while keeping the outer data short. When `PrecompiledContracts.extractBytesArray`/`extractSigArray` executes `new byte[len][]` at [5](#0-4) , the JVM attempts a multi-gigabyte allocation, which can trigger `OutOfMemoryError` uncaught in `ValidateMultiSign.execute` at [6](#0-5) .

---

**Note on confidence**: I was unable to fully verify how the top-level VM opcode dispatcher (`Program.java`, single match for the precompiled-contract call site) handles a propagated `Error`/uncaught runtime exception from a precompile's `execute()` — I could not read that call site before running out of tool iterations. It's possible java-tron's transaction-processing loop catches broad `Throwable` at a higher level and merely reverts the single transaction rather than crashing the node process, which would reduce this to a lower-severity DoS-per-transaction rather than a node crash. I flag this as the main uncertainty in the impact assessment; a background Devin session with full read access to `Program.java`'s call-frame exception handling would be needed to confirm whether `OutOfMemoryError`/`ArrayIndexOutOfBoundsException` from this path can escape per-transaction isolation.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L399-411)
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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1057-1074)
```java
      DataWord[] words = DataWord.parseArray(rawData);
      byte[] address = words[0].toTronAddress();
      int permissionId = words[1].intValueSafe();
      byte[] data = words[2].getData();

      byte[] combine = ByteUtil.merge(address, ByteArray.fromInt(permissionId), data);
      byte[] hash = Sha256Hash.hash(CommonParameter
          .getInstance().isECKeyCryptoEngine(), combine);

      if (VMConfig.allowTvmSelfdestructRestriction()) {
        int sigArraySize = words[words[3].intValueSafe() / WORD_SIZE].intValueSafe();
        if (sigArraySize > MAX_SIZE) {
          return Pair.of(true, DATA_FALSE);
        }
      }
      byte[][] signatures = VMConfig.allowTvmSelfdestructRestriction() ?
          extractSigArray(words, words[3].intValueSafe() / WORD_SIZE, rawData) :
          extractBytesArray(words, words[3].intValueSafe() / WORD_SIZE, rawData);
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1162-1181)
```java
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
        return Pair.of(true, DATA_FALSE);
      }
```
