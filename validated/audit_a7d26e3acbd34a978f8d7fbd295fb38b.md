### Title
Unbounded array allocation from attacker-controlled length in `BatchValidateSign`/`ValidateMultiSign` precompiles causes OOM/node crash - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
The `BatchValidateSign` (precompile `0x09`) and `ValidateMultiSign` (precompile `0x0a`) TVM precompiled contracts decode attacker-supplied calldata into `DataWord[]` and then use a raw, unvalidated length field taken directly from that calldata to allocate Java arrays (`new byte[len][]`) before any bound check is applied. This mirrors the CVE-2018-14618 bug class: a user-controlled length value is used to size a heap allocation without first validating that the value is sane relative to the actual input size, leading to a crash/DoS when the length is maliciously huge.

### Finding Description
`extractBytesArray` and `extractBytes32Array` read a length word straight from the caller-supplied `data`/`words` array and immediately allocate an array of that size: [1](#0-0) [2](#0-1) 

In `BatchValidateSign.doExecute`, the `addresses` array is built via `extractBytes32Array` **unconditionally**, and the `signatures` array is built via the unchecked `extractBytesArray` path whenever the `allowTvmSelfdestructRestriction` feature switch is off; the length sanity check (`sigArraySize > MAX_SIZE`) is only performed inside an `if (VMConfig.allowTvmSelfdestructRestriction())` guard, so on any chain/config where that switch has not been (or is not yet) activated, the raw unvalidated length flows straight into the allocation: [3](#0-2) 

The same pattern appears in `ValidateMultiSign.execute`: [4](#0-3) 

Neither `extractBytesArray` nor `extractBytes32Array` clamps `len` to `MAX_SIZE` (16 for `BatchValidateSign`, 5 for `ValidateMultiSign`) before allocating; the length only comes from a 32-byte word inside the attacker-controlled calldata (`words[offset].intValueSafe()`), which can be set to any value up to `Integer.MAX_VALUE`. This is directly analogous to the curl NTLM bug where a length taken from untrusted input is used to size a heap buffer without a bounds check first.

Note that the newer `isValidAbiEncoding` check gated by `VMConfig.allowTvmOsaka()` only validates that the *overall calldata byte length* is a multiple of the expected header/item word sizes; it does not validate the *value encoded in the internal length word* against the real content size, so a short, structurally-valid calldata can still carry a huge fabricated array-length field. [5](#0-4) 

### Impact Explanation
`new byte[len][]` with `len` near `Integer.MAX_VALUE` allocates an array of object references (~8 bytes each on 64-bit JVMs), instantly requesting multi-gigabyte heap space. This call sits outside the local try/catch that only wraps the account-permission-weighting logic in `ValidateMultiSign`, so the resulting `OutOfMemoryError`/huge GC pressure propagates up through `Program`/TVM execution. Because any account can trigger this by simply deploying a contract that `CALL`s the precompiled address `0x09` or `0x0a` with crafted calldata, this is reachable by an unprivileged contract caller and can degrade or crash the executing full node (denial of service), which the validation rules explicitly accept as in-scope impact ("node crash or halt").

### Likelihood Explanation
The vulnerable branch is only taken when `VMConfig.allowTvmSelfdestructRestriction()` returns `false`. This is a governance-activated (proposal-driven) hard-fork switch tracked in `DynamicPropertiesStore`/`ProposalUtil`. On networks (private chains, consortium chains, or a mainnet/testnet instance that has not yet processed the activating proposal) where this switch has not been turned on, the bug is trivially and remotely triggerable by any transaction sender with no special privileges — a single crafted `CALL` to the precompile is sufficient. I was not able to confirm from the available index whether this switch is currently forced-on for the shipped default configuration; if it is already permanently active on the target deployment, this exact code path is unreachable and the risk is limited to non-default/pre-fork deployments.

### Recommendation
Validate the declared array length against `MAX_SIZE` (and against the remaining calldata size) in `extractBytesArray` and `extractBytes32Array` themselves, unconditionally, before allocating — not only inside the `allowTvmSelfdestructRestriction`-gated branch. This makes the fix independent of feature-flag activation state and closes the gap left by `isValidAbiEncoding`, which only checks aggregate calldata size, not internal length fields.

### Proof of Concept
1. Craft calldata for `batchvalidatesign(bytes32,bytes[],address[])` (or `validatemultisign`) where the tail-array header word (the `bytes[]`/length word at the offset referenced by `words[1]`/`words[3]`) is set to a very large value (e.g. `0x7FFFFFFF`) while keeping the rest of the calldata short/otherwise structurally valid.
2. Deploy a trivial contract that performs a `CALL` (or `STATICCALL`) to address `0x0000...09` (or `...0a`) with that calldata.
3. On a node where `allowTvmSelfdestructRestriction` is not active, `extractBytesArray`/`extractBytes32Array` executes `new byte[0x7FFFFFFF][]`, immediately throwing `OutOfMemoryError` (or causing severe GC thrashing) inside TVM execution for that transaction, potentially destabilizing or crashing the node process.

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
