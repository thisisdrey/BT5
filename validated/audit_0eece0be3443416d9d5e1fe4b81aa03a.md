### Title
Unbounded array allocation in TVM precompile signature-array extraction leads to CPU/memory exhaustion - ([File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java])

### Summary
`PrecompiledContracts.extractBytesArray`, `extractSigArray`, and `extractBytes32Array` take an attacker-controlled length word straight out of calldata and use it, unchecked, both as the size of a newly allocated `byte[][]` and as the terminating bound of a `for` loop before any upper-bound validation occurs. This is analogous to the SystemDS CVE-2022-26477 pattern: a "controllable variable" is used as the loop/array-size bound with no upper limit, enabling CPU/memory exhaustion.

### Finding Description
`extractBytesArray`, `extractSigArray`, and `extractBytes32Array` read `len` from `words[offset].intValueSafe()` and immediately allocate `new byte[len][]`, then loop `for (int i = 0; i < len; i++)`, reading further words/derived offsets to fill it: [1](#0-0) 

These helpers are invoked from the `ValidateMultiSign` and `BatchValidateSign` precompiled contracts. The `MAX_SIZE` bound check on the extracted array length is only performed **after** extraction has already happened (`signatures.length > MAX_SIZE`), and the pre-check via `sigArraySize`/`addrArraySize` is gated behind `VMConfig.allowTvmSelfdestructRestriction()`: [2](#0-1) [3](#0-2) 

When `allowTvmSelfdestructRestriction` is not active (its default before the corresponding hard-fork activation), `len` flows directly from calldata into `new byte[len][]` and the subsequent loop with zero validation — the same "controllable termination bound" bug class flagged in the SystemDS advisory. Even when the restriction feature is active, `extractBytes32Array` (used for the address array in `BatchValidateSign`) has **no** corresponding pre-check at all — only `sigArraySize`/`addrArraySize` derived from the *same* words are checked, but `extractBytes32Array` itself never validates its `len` before allocating: [4](#0-3) [5](#0-4) 

### Impact Explanation
Any account can call the `ValidateMultiSign` or `BatchValidateSign` precompile addresses via a CALL opcode from a deployed contract, or directly via `eth_call`/`TriggerConstantContract`. By crafting the ABI-encoded length word for the signature/address array to a large value (bounded only by `int`, i.e. up to `Integer.MAX_VALUE`), the node is forced to allocate an oversized `byte[len][]` and iterate over it while resolving further offsets from the same calldata, before any size cap is enforced. This is CWE-400 resource exhaustion: it can trigger large heap allocation / GC pressure or `OutOfMemoryError` on every full node and validator that must re-execute the transaction as part of consensus, which is a stronger impact than the original library-local DoS in SystemDS because it is consensus-critical (all nodes execute identical transactions).

### Likelihood Explanation
Likelihood is high on chains/testnets where `ALLOW_TVM_SELFDESTRUCT_RESTRICTION` has not yet been activated (the pre-check is entirely absent), and moderate-to-high even post-activation because `extractBytes32Array`'s `len` is never itself bounded before allocation. The attack requires only a single, ordinary signed transaction or `eth_call`/constant-call request — no special privileges.

### Recommendation
Validate the extracted array length against `MAX_SIZE` (and against the remaining `words.length`) **before** allocating `new byte[len][]` or entering the loop in `extractBytesArray`, `extractSigArray`, and `extractBytes32Array`, rather than only checking the resulting array's `.length` after allocation. Apply the same pre-allocation bound unconditionally, not only when `VMConfig.allowTvmSelfdestructRestriction()` is enabled.

### Proof of Concept
1. Deploy or call (via `TriggerConstantContract`/`eth_call`) the `ValidateMultiSign` precompile address with hand-crafted calldata where the ABI header word encoding the signature-array length (`words[3]`-derived offset) is set to a very large value (e.g. `0x7FFFFFFF`).
2. On a network where `ALLOW_TVM_SELFDESTRUCT_RESTRICTION` is not active, `extractBytesArray` is invoked with this attacker-controlled `len`, executing `new byte[len][]` and the subsequent loop before the `MAX_SIZE` check at line 1076 is ever reached.
3. Repeat with `BatchValidateSign`, targeting `extractBytes32Array`'s address-array length word, which has no pre-check regardless of the feature flag.
4. Observe node-side memory/CPU spikes or `OutOfMemoryError` while processing the transaction.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L390-426)
```java
  private static byte[][] extractBytes32Array(DataWord[] words, int offset) {
    int len = words[offset].intValueSafe();
    byte[][] bytes32Array = new byte[len][];
    for (int i = 0; i < len; i++) {
      bytes32Array[i] = words[offset + i + 1].getData();
    }
    return bytes32Array;
  }

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1051-1078)
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
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1156-1181)
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
      int cnt = signatures.length;
      if (cnt == 0 || cnt > MAX_SIZE || signatures.length != addresses.length) {
        return Pair.of(true, DATA_FALSE);
      }
```
