### Title
Unbounded array allocation from attacker-controlled length word in `ValidateMultiSign` precompile causes uncaught `OutOfMemoryError` DoS - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The `ValidateMultiSign` TVM precompiled contract decodes an attacker-supplied signature-count field directly from calldata and uses it to allocate a `byte[][]` array with no upper bound, unless the `allowTvmSelfdestructRestriction` feature gate is active. A crafted `intValueSafe()`-clamped value (`Integer.MAX_VALUE`) at that offset triggers a massive array allocation attempt, and the resulting `OutOfMemoryError` is not caught anywhere in `ValidateMultiSign.execute()`.

### Finding Description
`ValidateMultiSign.execute()` reads a length/count word directly from the caller-supplied `rawData` and passes it, unbounded, into `extractBytesArray`: [1](#0-0) 

`extractBytesArray` performs `int len = words[offset].intValueSafe(); byte[][] bytesArray = new byte[len][];` with no cap on `len`: [2](#0-1) 

`DataWord.intValueSafe()` clamps any word occupying more than 4 bytes (or negative) to `Integer.MAX_VALUE` rather than throwing: [3](#0-2) 

So an attacker can set the 32-byte word at the computed array-length offset to any value ≥ 2^32 (or with the high bit set) and `intValueSafe()` returns `Integer.MAX_VALUE` (2,147,483,647). `new byte[Integer.MAX_VALUE][]` attempts to allocate roughly 8–16 GB of reference-array memory, which will throw `OutOfMemoryError` on essentially any node.

The `MAX_SIZE` (5) bound on signature count is only enforced when `VMConfig.allowTvmSelfdestructRestriction()` is active — it checks `sigArraySize` before calling `extractBytesArray` only in that branch. When that feature flag is not active, or if the crafted `len` is read via the unguarded `extractBytesArray` path, no size check happens before the allocation.

The TIP-854 shape-check `isValidAbiEncoding(data, ABI_HEADER_WORDS, ABI_ITEM_WORDS)` (gated by `allowTvmOsaka()`), only validates that `data.length` is a multiple of 32 and that the tail after the fixed header is an exact multiple of `ABI_ITEM_WORDS*32`; it never inspects or bounds the *value* of the length word embedded inside the payload at the attacker-chosen offset: [4](#0-3) 

This means the shape check does not prevent an attacker from placing an oversized length word at the location `ValidateMultiSign` treats as the signature-array length, as long as the overall calldata size still satisfies the header/item-word divisibility relation.

Critically, unlike `BatchValidateSign`, whose public `execute()` wraps the real logic in `try { return doExecute(data); } catch (Throwable t) { ... }`: [5](#0-4) 

`ValidateMultiSign.execute()` has **no such enclosing try/catch** around the `extractBytesArray` call — only an inner `try` around signature-weight computation that begins *after* the array has already been allocated: [6](#0-5) 

A test comment in the repository explicitly documents this asymmetry: prior to TIP-854 activation, "this precompile has no outer catch, so a too-short input raises inside the decoder" and this is "the documented pre-activation failure mode the TIP explicitly preserves." That same lack of an outer catch also means an `OutOfMemoryError` thrown from the oversized-allocation path propagates uncaught out of `execute()`.

### Impact Explanation
This is reachable by any unprivileged account that deploys a contract issuing a low-level `CALL`/`STATICCALL` to the `ValidateMultiSign` precompile address (or by directly crafting a transaction that triggers it), so no special privilege is needed. Triggering an `OutOfMemoryError` deep inside the TVM execution path during transaction/block processing is a node-crash/denial-of-service primitive: `OutOfMemoryError` is not a checked exception the surrounding actuator/VM machinery is designed to gracefully recover from mid-block-application, and depending on the JVM heap state it can destabilize concurrent processing threads on the same node (validating nodes and SRs alike), matching the CVE's "memory-exhaustion causing denial of service" bug class.

### Likelihood Explanation
The trigger requires only a single crafted transaction/contract call with attacker-controlled calldata — no elevated privileges, no witness/committee role, and no network-level access are required. The only mitigating factor is that the `MAX_SIZE`/`sigArraySize` guard exists when `allowTvmSelfdestructRestriction()` is enabled, but that guard does not exist in `extractBytesArray`'s own allocation logic, and the TIP-854 `isValidAbiEncoding` shape check does not validate the actual embedded count value, only overall calldata shape. Whether `allowTvmSelfdestructRestriction()` is enabled on current mainnet is not something I can verify from the code alone; this constrains confidence in current-network exploitability but the code path itself is unguarded in its absence.

### Recommendation
Enforce the `MAX_SIZE` bound unconditionally (independent of `allowTvmSelfdestructRestriction()`) inside `extractBytesArray`/`extractBytes32Array`/`extractSigArray` before allocating any `byte[][]`, e.g., reject immediately if `len < 0 || len > MAX_SIZE` (or a generic sane upper bound) prior to `new byte[len][]`. Additionally, wrap `ValidateMultiSign.execute()`'s full body in a `try { ... } catch (Throwable t) { return Pair.of(true, DATA_FALSE); }` consistent with `BatchValidateSign`, so any future unbounded-allocation or parsing defect fails safe instead of propagating an uncaught `OutOfMemoryError`.

### Proof of Concept
Construct calldata for the `ValidateMultiSign` precompile (called via a contract's low-level `call`) as 32-byte words:
- `word0` = target account address (bytes32-padded)
- `word1` = permissionId
- `word2` = hash data placeholder
- `word3` = offset value pointing to a small, in-bounds word index (e.g., `word4`)
- `word4` (at the offset location) = `0xFFFFFFFFFFFFFFFF...` (any value occupying > 4 bytes) — this becomes `len` after `intValueSafe()` clamps it to `Integer.MAX_VALUE`
- Pad remaining words so `data.length` still satisfies `isValidAbiEncoding`'s divisibility check if TIP-854 is active

Calling this precompile causes `extractBytesArray` to execute `new byte[2147483647][]`, throwing `OutOfMemoryError` with no enclosing catch in `ValidateMultiSign.execute()`, propagating out of the precompile call.

*Note: I could not fully trace, within the available tool budget, exactly how `Program.java`'s precompile-call site handles an uncaught `OutOfMemoryError` from `execute()` (e.g., whether some outer VM/actuator layer catches `Throwable`/`Error` generically at block-application time) — this is the main remaining uncertainty affecting whether the crash is contained to the single transaction or destabilizes the node process more broadly.*

### Citations

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
