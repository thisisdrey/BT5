### Title
Unbounded, attacker-controlled array-length words in `BatchValidateSign`/`ValidateMultiSign` precompiles cause unchecked `new byte[len][]` allocation — OOM/node-crash DoS - ([File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java])

### Summary
`PrecompiledContracts.extractBytes32Array`, `extractBytesArray`, and `extractSigArray` read a 32‑byte "array length" word directly out of attacker‑supplied calldata and pass it straight into a Java array allocation (`new byte[len][]`) with no upper bound check, mirroring the Pillow `BdfFontFile` pattern of using an untrusted size field to drive `Image.new()` without the `_decompression_bomb_check()` gate. The bound (`MAX_SIZE`) is only enforced when the `allowTvmSelfdestructRestriction` feature flag is active, and even then only for `BatchValidateSign`'s two size fields — `extractBytes32Array` itself is called unconditionally and never validates `len` before allocating.

### Finding Description
`extractBytes32Array` reads `len` from the calldata word at `offset` and immediately allocates a jagged array of that size with zero validation: [1](#0-0) 

`extractBytesArray` and `extractSigArray` have the same pattern — only an `offset > words.length - 1` guard, but no cap on `len` before `new byte[len][]`: [2](#0-1) 

These helpers are invoked from `ValidateMultiSign.execute` and `BatchValidateSign.doExecute`, which are the TVM precompiled contracts reachable from any deployed contract via a `CALL`/`STATICCALL` from any unprivileged `TriggerSmartContract` transaction: [3](#0-2) [4](#0-3) 

The `MAX_SIZE` check that is supposed to bound `len` only runs when `VMConfig.allowTvmSelfdestructRestriction()` is enabled, and it protects only the specific `sigArraySize`/`addrArraySize` words used for that gate — it does not change the fact that `extractBytes32Array` (used for the `addresses` array in `BatchValidateSign`) is called unconditionally right after, with the same untrusted `len` value flowing straight into `new byte[len][]` with no cap applied inside the helper itself. Because `getEnergyForData` derives its energy charge from the *physical* `data.length` of the calldata (`(data.length / WORD_SIZE - 5) / 6`), an attacker can submit a small calldata buffer (a handful of words) while setting the length word pointed to by `words[2]`/`words[3]` to an arbitrary large 32-bit value (e.g. `0x7FFFFFFF`), since `intValueSafe()` simply clamps a `DataWord` into an `int`. This decouples the actual energy paid from the size of the allocation attempted.

### Impact Explanation
`len` up to `Integer.MAX_VALUE` (~2.1 billion) passed to `new byte[len][]` attempts to allocate an object-reference array of roughly 8–16 GB on a 64-bit JVM, which throws `OutOfMemoryError`. Depending on JVM state/heap headroom this can crash or destabilize the node process executing/validating the block (all full nodes that execute the transaction, i.e. the entire validating network), making this a broadcastable-transaction DoS against block production/serving — a node crash/halt condition. This is reachable by any account able to deploy and call a trivial contract, with attacker cost proportional to a tiny, mis-declared calldata length rather than to the requested allocation size.

### Likelihood Explanation
High. `ValidateMultiSign` (address `0x66`/analogous) and `BatchValidateSign` are public precompiles reachable from any Solidity contract via a low-level call; no special permission, large deposit, or privileged role is required — just a `TriggerSmartContractTransaction` from any funded account with enough energy for the (attacker-controlled, artificially small) declared word count. Constructing the malicious ABI encoding requires no more sophistication than manually crafting a `bytes[]`/`address[]` calldata header, analogous in triviality to the 270-byte malicious BDF file in the original report.

### Recommendation
- Add an explicit upper bound check (e.g. `len < 0 || len > MAX_SIZE` or a small sane ceiling) inside `extractBytes32Array`, `extractBytesArray`, and `extractSigArray` themselves, before performing `new byte[len][]`, independent of any feature-flag gating.
- Additionally validate that `len` is consistent with the actual remaining `words.length`/`data.length` before allocation (as is already partially done via `isValidAbiEncoding`), so an allocation request can never exceed what the supplied calldata could possibly encode.
- Make these checks unconditional (not gated behind `VMConfig.allowTvmSelfdestructRestriction()`), since the underlying allocation primitive is unsafe regardless of that chain parameter's activation state.

### Proof of Concept
1. Deploy a trivial contract that performs a raw `call` to the `BatchValidateSign` precompile address with hand-crafted calldata: 5 header words (`hash`, offset-to-signatures, offset-to-addresses, ...) sized just large enough to satisfy `isValidAbiEncoding`/array bounds, but with the word at the `addresses` array offset set to `0x7FFFFFFF` instead of a small real count.
2. Call this function via a normal `TriggerSmartContract` transaction from any account with sufficient energy for the (small) `data.length`-based energy charge computed by `getEnergyForData` (`(data.length / WORD_SIZE - 5) / 6`).
3. Execution reaches `extractBytes32Array(words, offset)`, which executes `new byte[2147483647][]`, throwing `OutOfMemoryError` in the node's VM execution thread, since the size passed was never validated against a sane bound before allocation — directly analogous to `Image.new(mode, (width, height))` being invoked in `BdfFontFile.bdf_char()` without `_decompression_bomb_check()`.

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1051-1074)
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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1156-1177)
```java
    private Pair<Boolean, byte[]> doExecute(byte[] data)
        throws InterruptedException, ExecutionException {
      if (VMConfig.allowTvmOsaka()
          && !isValidAbiEncoding(data, ABI_HEADER_WORDS, ABI_ITEM_WORDS)) {
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
```
