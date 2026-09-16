### Title
Unbounded array-length precompile input causes disproportionate memory allocation and node crash - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The `ValidateMultiSign` and `BatchValidateSign` TVM precompiled contracts (reachable by any account issuing a `CALL`/`STATICCALL` to precompile addresses `0x...9` and `0x100...`) parse an attacker-controlled 32-byte "array length" word directly out of call data and use it to allocate a `byte[][]` of that size, without any upper bound check on the legacy code path. A single small transaction can therefore trigger allocation of billions of array-reference slots, mirroring the BIND TKEY bug class where a small malicious packet forces disproportionate server-side memory consumption.

### Finding Description
`extractBytesArray` and `extractBytes32Array` read the array length straight from `DataWord[] words` supplied by the caller and immediately allocate a Java array of that size before validating it against the actual size of `data`: [1](#0-0) 

`extractSigArray` has the same unchecked pattern: [2](#0-1) 

These helpers are invoked from `ValidateMultiSign.execute` and `BatchValidateSign.doExecute`, both of which are precompiled contracts reachable from ordinary contract calls: [3](#0-2) [4](#0-3) 

The size guard (`sigArraySize > MAX_SIZE` / `addrArraySize > MAX_SIZE`) is applied **only** when `VMConfig.allowTvmSelfdestructRestriction()` is active; on that branch the code calls the bounded `extractSigArray`. On the legacy branch (`allowTvmSelfdestructRestriction()` false), the code calls the unbounded `extractBytesArray`/`extractBytes32Array`, so `len = words[offset].intValueSafe()` — a value fully controlled by the calldata — is used directly as the size of `new byte[len][]` with no cap relative to the actual input length. Because `intValueSafe()` can return values up to `Integer.MAX_VALUE`, a small calldata payload can request allocation of a multi-gigabyte array of object references before any subsequent index-out-of-bounds exception would occur, producing an `OutOfMemoryError`/node stall from trivial-sized input — the same "small malicious packet, large server-side allocation" shape as ALPINE-CVE-2026-3039.

I was not able to confirm from the excerpts retrieved whether `allowTvmSelfdestructRestriction()` is unconditionally forced to `true` (i.e., the vulnerable legacy branch is dead code on all currently running networks) or whether it is a runtime/committee-controlled flag that could still be `false` on some deployed chain (e.g., a private chain, test network, or a network state prior to activation of the corresponding TIP). This is a material factor for real-world exploitability and should be verified directly in `VMConfig.java` / `DynamicPropertiesStore.java` before treating this as immediately exploitable on TRON mainnet.

### Impact Explanation
If the legacy branch is reachable (flag not activated), any unprivileged account can send a cheap, small transaction that triggers a huge array allocation inside precompile execution, causing `OutOfMemoryError` in the JVM running the full node — a resource-exhaustion / crash condition affecting block execution for all nodes that process the transaction, i.e., a network-wide denial-of-service, consistent with the "node crash or halt" acceptance criterion.

### Likelihood Explanation
Likelihood depends entirely on whether `allowTvmSelfdestructRestriction()` defaults to disabled on any network still processing transactions. If it is permanently enabled today, this is legacy dead code with no current impact. If it can be `false` (e.g., unactivated proposal on a side/test chain, or historical replay), exploitation requires only crafting calldata for `validatemultisign`/`batchvalidatesign` with a maximum-length header word — trivial for any transaction broadcaster.

### Recommendation
Remove the conditional gating and always validate array-length fields against `MAX_SIZE` (and against the real remaining calldata length) before allocating `byte[][]` arrays in `extractBytesArray`, `extractBytes32Array`, and `extractSigArray`, regardless of the `allowTvmSelfdestructRestriction()` flag state. Alternatively, delete the unbounded legacy branch entirely once the corresponding hard fork is confirmed active on all supported networks.

### Proof of Concept
Construct calldata for `validatemultisign(address,uint256,bytes32,bytes[])` (or `batchvalidatesign(bytes32,bytes[],address[])`) where the length word for the `bytes[]`/`address[]` array is set to `0x7FFFFFFF` while the remaining calldata is only a few hundred bytes. On the code path where `allowTvmSelfdestructRestriction()` is false, `execute()`/`doExecute()` calls `extractBytesArray`/`extractBytes32Array`, which immediately execute `new byte[0x7FFFFFFF][]`, attempting to allocate on the order of tens of gigabytes of reference slots and triggering an `OutOfMemoryError` for that node's transaction execution thread. [5](#0-4)

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1052-1078)
```java
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
