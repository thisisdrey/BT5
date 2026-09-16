## Title
Uncaught `ArrayIndexOutOfBoundsException` in `ValidateMultiSign` precompile from unbounded attacker-controlled array length - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
The `ValidateMultiSign` precompiled contract (invoked via a TVM `CALL`/`STATICCALL` to a fixed precompile address) decodes attacker-supplied calldata into a `DataWord[]` and derives array-index offsets and loop bounds directly from that data, then reads `words[offset + i + 1]` in `extractBytesArray`/`extractSigArray` without validating that the computed length keeps the access inside the `words` array bounds. This mirrors the TensorFlow bug class: an index/length value taken from untrusted input is used to index an array with only a partial (offset-only) precondition check, while the length itself remains unchecked, producing an out-of-bounds array access.

### Finding Description
`extractBytesArray` and `extractSigArray` only check that the starting `offset` is within bounds: [1](#0-0) [2](#0-1) 

The `len` in both functions comes straight from `words[offset].intValueSafe()` — fully attacker-controlled — and is then used unconditionally as the loop bound for `words[offset + i + 1]` and `words[offset + bytesOffset + 1]`, with no check that `offset + i + 1 < words.length`. Just like the TensorFlow report's `dirs[dir_index]` where `dir_index` is incremented outside any lambda bounds check, here the loop index `i` walks past the actual array length because only the *starting* offset — not the full range implied by `len` — was validated.

`ValidateMultiSign.execute()` calls these helpers **before** entering its only `try { ... } catch (Throwable t)` block: [3](#0-2) 

Because the `try` only starts at line 1082 (`AccountCapsule account = ...` / `try { Permission permission = ...`), any `ArrayIndexOutOfBoundsException` thrown from `extractSigArray`/`extractBytesArray` at lines 1072–1074, or from `words[0]`, `words[1]`, `words[3]` accesses at lines 1058–1067, is **not caught inside `execute()`** and propagates out of the precompiled-contract call.

When `VMConfig.allowTvmOsaka()` is disabled (or even when enabled but `isValidAbiEncoding` accepts a length-consistent-but-content-malicious layout), the `words[3].intValueSafe()/WORD_SIZE` offset and the derived `len` are attacker-chosen: a contract can craft calldata whose header words assert a huge signature-array length while the actual `rawData` buffer is much shorter, causing `words[offset + i + 1]` to walk off the end of the parsed `DataWord[]`.

### Impact Explanation
An uncaught `ArrayIndexOutOfBoundsException` (a `RuntimeException`) is propagated out of the precompile execution path. It is eventually caught by the VM's opcode-dispatch loop (`VM.play`'s `catch (RuntimeException e)`), which spends all remaining energy and stops the program — so in the current call chain this most likely degrades to a failed/reverted transaction rather than a node crash, since the outer interpreter loop treats it as a generic VM runtime exception. Because I could not fully trace whether every calling context (e.g., constant/view calls via `TriggerConstantContract`, or other node-side invocation paths that call precompile `execute()` directly without going through `VM.play()`) wraps this in an equivalent broad catch, I cannot rule out a path where this exception escapes uncaught to a node-level RPC handler and produces an unhandled node crash or an API failure. This uncertainty should be verified with a live/debug run before final severity assignment.

### Likelihood Explanation
High reachability: `ValidateMultiSign` is a standard TVM precompile reachable by any contract deployer or contract caller via a `CALL`/`STATICCALL` to its fixed address with attacker-fully-controlled calldata bytes — no special permission is required. Constructing calldata where the declared array length exceeds the actual data buffer size is trivial (a few crafted 32-byte words).

### Recommendation
Add explicit bounds validation in `extractBytesArray`/`extractSigArray` before using `len` as a loop bound — reject (return empty / false) whenever `offset + len + 1 > words.length` or, per-iteration, whenever `offset + i + 1 >= words.length`, mirroring the fix TensorFlow applied (fully validating array-index invariants rather than trusting attacker-supplied lengths). Additionally, widen the `try/catch` in `ValidateMultiSign.execute()` (and `BatchValidateSign.doExecute()`, which has the same unguarded pattern) to wrap the `extractSigArray`/`extractBytesArray` calls and the `words[...]` header accesses, so any decoding exception degrades gracefully to `Pair.of(true, DATA_FALSE)` instead of propagating as an uncaught runtime exception.

### Proof of Concept
1. Deploy any contract that performs a low-level `call` to the `ValidateMultiSign` precompile address with hand-crafted calldata:
   - `words[0]` = arbitrary account address
   - `words[1]` = arbitrary permission id
   - `words[2]` = arbitrary data word
   - `words[3]` = an offset value (e.g., `4 * WORD_SIZE`) pointing past the header
   - Provide **no further words** in `rawData` (i.e., `rawData.length == 4*32`), so `DataWord.parseArray` yields exactly 4 `DataWord`s.
   - At the computed offset (`words[3].intValueSafe()/WORD_SIZE == 4`), `words[4]` does not exist — but even a value that resolves `offset == words.length` bypasses the `offset > words.length - 1` guard boundary condition, or with `allowTvmSelfdestructRestriction` disabled, an offset that resolves in-bounds but whose `len = words[offset].intValueSafe()` is set arbitrarily large (e.g., `0xFFFFFFFF`) forces `extractBytesArray`'s loop to index `words[offset + i + 1]` far beyond `words.length`.
2. `extractBytesArray`/`extractSigArray` throws `ArrayIndexOutOfBoundsException`, which is not caught by `ValidateMultiSign.execute()`'s scoped `try/catch` (only covering lines 1082+), propagating to the VM opcode loop as an uncaught `RuntimeException`.

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
