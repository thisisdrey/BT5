### Title
Unbounded array allocation from attacker-controlled length field in TVM precompiled contracts `ValidateMultiSign`/`BatchValidateSign` - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
The reported CVE describes a Virtuoso DoS where a crafted, cheap-to-send input drives an internal hash/array structure to allocate based on an attacker-controlled size field, crashing the server. The java-tron analog is the `extractBytesArray`/`extractBytes32Array` helpers used by the `ValidateMultiSign` and `BatchValidateSign` TVM precompiled contracts [1](#0-0) . These helpers read a 32-byte "length" word straight out of attacker-supplied call data and immediately allocate a Java array of that size, with no upper bound check unless a specific hardfork flag (`VMConfig.allowTvmSelfdestructRestriction()`) is active and taken on the exact code path.

### Finding Description
`extractBytes32Array` and `extractBytesArray` compute `len` from `words[offset].intValueSafe()` — a value fully controlled by the caller inside the precompile's ABI-encoded input — and immediately do `new byte[len][]`: [1](#0-0) 

`ValidateMultiSign.execute` only bounds the signature-array size (`sigArraySize > MAX_SIZE`) when `VMConfig.allowTvmSelfdestructRestriction()` is true, and in that branch it calls the presumably-guarded `extractSigArray` instead of `extractBytesArray`. When that hardfork flag is not active for the code path taken (or in the `BatchValidateSign` case, the bound only guards `sigArraySize`/`addrArraySize` read from `words[...]`, not the actual bytes read from `data` during extraction), the unguarded `extractBytesArray`/`extractBytes32Array` path is used with no cap: [2](#0-1) [3](#0-2) 

Because the "length" is just one 32-byte word embedded in the call data (not the actual size of the transmitted data), an attacker can submit a very small transaction (a handful of 32-byte words) that encodes an enormous count (e.g., close to `Integer.MAX_VALUE`), forcing the precompile to attempt allocation of a huge `byte[][]` array. Energy cost for these precompiles (`getEnergyForData`) is derived only from `data.length / WORD_SIZE`, i.e., the actual (small) call data size, not from the attacker-chosen length field, so the attacker pays energy proportional to a tiny payload while the JVM attempts a massive heap allocation.

### Impact Explanation
An oversized `new byte[len][]` allocation for large `len` throws `OutOfMemoryError`. Although the precompile's outer `execute` wraps calls in `catch (Throwable t)`, an `OutOfMemoryError` thrown during a large allocation attempt can still cause significant GC pressure/heap exhaustion across the JVM before or while being caught, since other threads (block processing, RPC handling, etc.) share the same heap. This can degrade or crash the node process, or cause the node to no longer service the TVM/JSON-RPC/HTTP query paths that route into it, matching the CVE's DoS class ("crafted input causes hash/array allocation issue leading to resource exhaustion / crash").

### Likelihood Explanation
The precompiles are reachable by any unprivileged account via a normal `TriggerSmartContract` call (or `staticcall`/`call` from any deployed contract) targeting the precompile address — no special privileges are required. The attack only requires constructing calldata with a small number of words, one of which encodes a large offset/length value, so the cost to the attacker (transaction fee, energy) is minimal relative to the JVM allocation triggered on the node.

### Recommendation
- Enforce the `MAX_SIZE` bound check unconditionally (not gated behind `VMConfig.allowTvmSelfdestructRestriction()`) before calling `extractBytesArray`/`extractBytes32Array` in both `ValidateMultiSign` and `BatchValidateSign`.
- Add a hard sanity cap inside `extractBytesArray`/`extractBytes32Array` themselves (e.g., reject `len` above a small constant, or verify `len` is consistent with `data.length`) so these low-level helpers are safe regardless of caller checks.
- Ensure `getEnergyForData` cannot be bypassed by data whose declared internal lengths are inconsistent with the physical `data.length`.

### Proof of Concept
Conceptual PoC (exact byte offsets require finalizing against the live ABI layout of these precompiles):
1. Craft a `TriggerSmartContract` transaction whose `data` targets the `BatchValidateSign` (or `ValidateMultiSign`) precompile address.
2. Encode only a few 32-byte words: a hash word, then offset words pointing to the signatures/addresses array, and set the length word at that offset to a very large value (e.g., `0x7FFFFFFF`).
3. Submit the transaction (or a `constant`/view call) targeting a node running with `allowTvmSelfdestructRestriction()` not yet enabled (or through `BatchValidateSign`'s array construction), forcing `new byte[len][]` with `len` ≈ 2^31, which the JVM will attempt to allocate.
4. Observe increased GC activity / `OutOfMemoryError` / node instability, achieved with a data payload only a few hundred bytes long and minimal energy cost.

**Note on confidence:** I was unable to fully confirm within the available search iterations (a) the exact current mainnet default of `VMConfig.allowTvmSelfdestructRestriction()` (i.e., whether it is already permanently enabled on production networks, which would restrict this to `BatchValidateSign`'s partially-checked path or historical/pre-fork nodes), and (b) the precise clamping behavior of `DataWord.intValueSafe()` for very large values. These should be verified against the live deployment/hardfork configuration and `DataWord.intValueSafe()` implementation before treating this as confirmed on current mainnet.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L390-412)
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
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1051-1080)
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
