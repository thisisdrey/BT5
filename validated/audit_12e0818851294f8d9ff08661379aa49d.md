## Finding: `ValidateMultiSign` precompile can throw an uncaught `ArrayIndexOutOfBoundsException` on crafted calldata offsets - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The CVE describes zziplib's `zzip_mem_entry_extra_block` trusting an attacker-controlled "extra field" offset/length inside a ZIP entry without validating it against the buffer bounds, causing an invalid memory read/crash. The structural analog in java-tron is the manual offset-based array parsing used by the `ValidateMultiSign`/`BatchValidateSign` TVM precompiles, where offsets embedded in the transaction's call data are used to index into a parsed `DataWord[]` array without full bounds validation.

### Finding Description
`extractBytesArray` and `extractSigArray` only guard the *initial* offset: [1](#0-0) 

They check `offset > words.length - 1` but never validate that `offset` is non-negative, nor that the derived `bytesOffset` (itself taken from attacker-controlled call data, `words[offset + i + 1].intValueSafe() / WORD_SIZE`) keeps `words[offset + bytesOffset + 1]` and the final `extractBytes` slice inside the bounds of `words`/`data`. This is functionally the same class of bug as the CVE: a length/offset field taken directly from untrusted input is used to index a buffer without full bounds checking.

`ValidateMultiSign.execute()` calls these extraction helpers *before* entering any try/catch: [2](#0-1) 

The `try { ... } catch (Throwable t)` block only wraps the code after the account lookup (permission/signature verification), not the `DataWord.parseArray`, `words[0..3]` accesses, or the `extractSigArray`/`extractBytesArray` calls. By contrast, `BatchValidateSign.execute()` wraps its *entire* `doExecute` call (including the equivalent extraction calls) in a top-level `try/catch (Throwable t)` that safely returns a zero result: [3](#0-2) 

The `isValidAbiEncoding` guard added for TIP-854 (`allowTvmOsaka`) only validates that the *overall* calldata length matches a `(header + n*item)*32` shape — it does not validate that internal pointer fields (e.g. `words[3]` in `ValidateMultiSign`, or the offsets they lead to) resolve to positions inside the parsed `words` array: [4](#0-3) 

So a transaction can satisfy the shape check while still encoding an out-of-range or negative sub-offset, which is exactly the missing "extra block" bounds validation described in the CVE.

### Impact Explanation
Any account can invoke `ValidateMultiSign` (address `0x...0a`, gated only by `VMConfig.allowTvmSolidity059()`, a long-active feature) from a contract call with attacker-controlled calldata. A crafted offset can drive `extractBytesArray`/`extractSigArray` to index outside the `words[]`/`data[]` arrays, throwing an uncaught `ArrayIndexOutOfBoundsException` from a code path in `ValidateMultiSign.execute()` that — unlike its sibling `BatchValidateSign` — has no enclosing exception handler. The project's own regression test (`testTip854OuterFrameContainment`) shows this exact scenario class (uncaught exceptions from these two precompiles) was previously a real concern requiring an explicit fix/guard, confirming the reachable impact of unhandled exceptions from this code path on VM/transaction execution.

### Likelihood Explanation
High reachability: this is triggered purely by a contract's `CALL`/`STATICCALL` to a well-known precompile address with attacker-supplied calldata — no privileged role or special network state is required, matching the CVE's "remote attacker with crafted input" profile.

### Recommendation
- Wrap the entire `ValidateMultiSign.execute()` body (including `DataWord.parseArray`, header-word access, and `extractSigArray`/`extractBytesArray` calls) in the same defensive `try/catch (Throwable)` pattern already used by `BatchValidateSign.execute()`.
- Harden `extractBytesArray`/`extractSigArray`/`extractBytes32Array` to explicitly validate that `offset`, `bytesOffset`, and all derived indices are non-negative and within `words.length`/`data.length` before use, rather than relying solely on the coarse `isValidAbiEncoding` shape check.

### Proof of Concept
Craft a `validateMultiSign(address,uint256,bytes,bytes[])`-style calldata where the dynamic "signatures" offset word (`words[3]`) is a large/negative-when-interpreted value such that `words[3].intValueSafe() / WORD_SIZE` yields an index outside the bounds of the parsed `words[]` array (or where an inner length/offset word inside the "array" region is set to a huge value), then submit it as a `TriggerSmartContract` call targeting the `validateMultiSign` precompile address. This drives execution into `extractBytesArray`/`extractSigArray` (`PrecompiledContracts.java:399-426`) before the encompassing `try` block in `ValidateMultiSign.execute()` (`PrecompiledContracts.java:1082`), causing an uncaught `ArrayIndexOutOfBoundsException` to propagate out of the precompile's `execute()` method.

### Citations

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L432-438)
```java
  private static boolean isValidAbiEncoding(byte[] data, int headerWords, int itemWords) {
    if (data == null || data.length % WORD_SIZE != 0) {
      return false;
    }
    long tail = subtractExact(data.length, multiplyExact(headerWords, WORD_SIZE));
    return tail > 0 && tail % multiplyExact(itemWords, WORD_SIZE) == 0;
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1051-1082)
```java
    @Override
    public Pair<Boolean, byte[]> execute(byte[] rawData) {
      if (VMConfig.allowTvmOsaka()
          && !isValidAbiEncoding(rawData, ABI_HEADER_WORDS, ABI_ITEM_WORDS)) {
        return Pair.of(false, EMPTY_BYTE_ARRAY);
      }
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

      if (signatures.length == 0 || signatures.length > MAX_SIZE) {
        return Pair.of(true, DATA_FALSE);
      }

      AccountCapsule account = this.getDeposit().getAccount(address);
      if (account != null) {
        try {
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1144-1154)
```java
    @Override
    public Pair<Boolean, byte[]> execute(byte[] data) {
      try {
        return doExecute(data);
      } catch (Throwable t) {
        if (t instanceof InterruptedException){
          Thread.currentThread().interrupt();
        }
        return Pair.of(true, new byte[WORD_SIZE]);
      }
    }
```
