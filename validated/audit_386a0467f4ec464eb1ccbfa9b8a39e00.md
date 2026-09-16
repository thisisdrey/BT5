Based on my research, I found a strong structural analog to the libheif bug class in java-tron's `ValidateMultiSign` precompiled contract, where an attacker-controlled length value flows unchecked into an array-allocation/copy primitive.

### Title
Unbounded attacker-controlled array length in `PrecompiledContracts.extractBytesArray` causes uncaught OutOfMemoryError / node crash - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
The libheif CVE root cause is a computed length derived from unvalidated/unclipped input that is fed directly into a raw memory-copy primitive (`memcpy`) without bounds checking, producing a crash. The analogous pattern in java-tron is `extractBytesArray`/`extractSigArray`, which derive an array length directly from raw ABI-encoded call data supplied by any contract caller and pass it, unclamped, into array allocation and `Arrays.copyOfRange` (Java's `memcpy` analog) before any bound is enforced in one of the two configurable code paths.

### Finding Description
`extractBytesArray` reads a length word directly from attacker-supplied bytes and immediately allocates an array of that size: [1](#0-0) 

`len` comes from `words[offset].intValueSafe()`, which for out-of-range/negative-looking words returns `Integer.MAX_VALUE` rather than a validated bound: [2](#0-1) 

This function is invoked from `ValidateMultiSign.execute()`. Whether the dangerous unbounded path is reached depends on the `allowTvmSelfdestructRestriction()` config flag: [3](#0-2) 

When the restriction flag is enabled, a size check (`sigArraySize > MAX_SIZE`) is applied *before* calling `extract*Array`, capping `len` at 5. When the flag is disabled, `extractBytesArray` is called with no prior bound on `len`, so `new byte[len][]` can attempt to allocate up to `Integer.MAX_VALUE` array slots directly from a single crafted transaction's call data — the same "invalid length flows straight into a copy/allocation primitive" pattern as the libheif `HeifPixelImage::overlay()` bug. Additionally, inside the per-element loop, `extractBytes` performs `Arrays.copyOfRange(data, offset, offset + len)` with `offset`/`len` computed from other unclamped `intValueSafe()` reads, so integer overflow in `(bytesOffset + offset + 2) * WORD_SIZE` can also produce out-of-range indices passed straight to the copy: [4](#0-3) 

Critically, unlike the sibling `BatchValidateSign` precompile, which wraps its entire `doExecute` in `catch (Throwable t)`: [5](#0-4) 

`ValidateMultiSign.execute()` has no such outer guard around the `extractBytesArray`/`extractSigArray` call; the `try/catch` only wraps the later signature-weight loop, so any `Error`/`RuntimeException` thrown while parsing the array (e.g. `OutOfMemoryError` from the huge allocation, or `ArrayIndexOutOfBoundsException`/`NegativeArraySizeException` from bad offsets) propagates out of the precompile uncaught.

### Impact Explanation
An unbounded `new byte[Integer.MAX_VALUE][]` allocation attempt (~8–16 GB of array-header memory) triggers `OutOfMemoryError`, which — because it is an `Error`, not an `Exception` — is not caught by ordinary `catch (Exception e)` handling and can escape typical VM exception wrappers, destabilizing or crashing the node process executing the block/transaction. This matches the "node crash or halt" impact bar, and is reachable by any unprivileged account simply calling the `ValidateMultiSign` precompiled contract address with crafted call data — no special privilege, SR/witness role, or prior state is required.

### Likelihood Explanation
Exploitability depends on the runtime value of `VMConfig.allowTvmSelfdestructRestriction()`. I could not fully verify from the indexed code whether this flag is unconditionally hardcoded to `true` on all currently supported networks/versions, or whether it can be `false` (e.g., pre-fork chains, private/test networks, or during a transition window). I was also unable to trace, within the tool budget, the top-level exception handling in the VM's precompiled-contract call site (`Program.java`, single match found) to confirm whether an `Error` thrown from `execute()` is swallowed there or propagates further up to crash the block-application thread — this materially affects whether the impact is "node halts" versus merely "single transaction reverts." Given this uncertainty, treat the crash-impact claim as unconfirmed pending inspection of the call site and current default value of `allowTvmSelfdestructRestriction()`.

### Recommendation
- In `extractBytesArray` and `extractSigArray`, validate `len` against `MAX_SIZE` (or another sane bound) and validate that `offset + i + 1 < words.length` before every `words[...]` access, regardless of the `allowTvmSelfdestructRestriction()` flag.
- Wrap the entirety of `ValidateMultiSign.execute()` in a `catch (Throwable t)` guard, mirroring `BatchValidateSign.execute()`, so malformed input degrades to `DATA_FALSE` rather than propagating an uncaught `Error`/`RuntimeException`.
- Bound-check `offset`/`len` arithmetic in `extractBytes` before calling `Arrays.copyOfRange` to avoid integer-overflow-driven invalid ranges.

### Proof of Concept
1. Deploy or use any contract that issues a raw `CALL` (or call directly via a signed transaction using `TriggerSmartContract`) to the `ValidateMultiSign` precompiled contract address.
2. Craft `rawData` such that: `words[3]` points to an offset word; the word at that offset (used as `len` in `extractBytesArray`) is set to a large value (e.g., 32 bytes of `0xFF` so `intValueSafe()` returns `Integer.MAX_VALUE`), and the deployment/network configuration has `allowTvmSelfdestructRestriction()` returning `false` (or is otherwise not gating this size).
3. `extractBytesArray` executes `new byte[Integer.MAX_VALUE][]`, throwing `OutOfMemoryError`, uncaught by `ValidateMultiSign.execute()`.
4. Observe whether the `Error` propagates past the VM's call-execution guard to the transaction/block-processing thread.

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L428-430)
```java
  private static byte[] extractBytes(byte[] data, int offset, int len) {
    return Arrays.copyOfRange(data, offset, offset + len);
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1051-1075)
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
