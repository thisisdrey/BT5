### Title
Unbounded array allocation from attacker-controlled ABI length words in `BatchValidateSign`/`ValidateMultiSign` precompiles causes uncontrolled memory allocation - ([File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java])

### Summary
Similar to CVE-2024-37358 (Apache James allocating unbounded memory from an attacker-supplied IMAP literal length), java-tron's `BatchValidateSign` and `ValidateMultiSign` TVM precompiled contracts parse calldata as ABI-encoded words and use an attacker-controlled 32-byte word directly as an array length to allocate a Java array, without validating it against the actual size of the input data, whenever `VMConfig.allowTvmSelfdestructRestriction()` is disabled.

### Finding Description
Both precompiles parse their raw calldata into `DataWord[] words` and then extract dynamic arrays via helper functions that trust a length field taken straight from calldata: [1](#0-0) 

`extractBytes32Array` and `extractBytesArray` both compute `int len = words[offset].intValueSafe();` and then immediately execute `new byte[len][]` **before** any loop bounds are checked against the real size of `words`/`data`. `intValueSafe()` can return values up to `Integer.MAX_VALUE` for a crafted calldata word.

In `BatchValidateSign.doExecute`, the size-limiting check (`MAX_SIZE = 16`) that would normally reject an oversized `len` is only performed when the chain parameter `VMConfig.allowTvmSelfdestructRestriction()` is enabled: [2](#0-1) 

When that flag is `false`, `extractBytesArray` (for `signatures`) and `extractBytes32Array` (for `addresses`, called unconditionally regardless of the flag) are invoked with the raw, unvalidated `len`, directly allocating `new byte[len][]` where `len` is attacker-chosen. The identical pattern exists in `ValidateMultiSign.execute`: [3](#0-2) 

A caller can trigger the CALL to this precompile from any smart-contract transaction (`batchvalidatesign`/`validatemultisign` calldata is fully attacker-controlled), setting the relevant ABI-offset word to a huge value (e.g. `0x7FFFFFFF`), forcing the JVM to attempt an allocation of billions of object-array slots before the code ever reaches the bounds-checking loop that would otherwise fail fast.

### Impact Explanation
Attempting to allocate an array of ~2^31 references (multiple GB, depending on JVM pointer compression) can trigger heavy GC activity, `OutOfMemoryError`, or a full-node pause. `BatchValidateSign.execute` wraps `doExecute` in a broad `catch (Throwable t)`, so `OutOfMemoryError` is theoretically caught per-call, but the JVM typically performs multiple full GC cycles before throwing OOM, causing a stop-the-world pause that affects the entire node process, not just the failing call. `ValidateMultiSign.execute` has no such outer catch at all for the array-allocation path shown, so the error can propagate further up. Both precompiles are reachable by any account issuing a normal signed transaction/contract call, matching the CWE-400/CWE-770 uncontrolled resource consumption class from the referenced advisory. This can degrade or crash node availability, impacting all users relying on that node (validators/SRs, API consumers).

### Likelihood Explanation
Exploitation requires only a single crafted transaction calling a contract that invokes `batchvalidatesign` or `validatemultisign` (both are public TVM precompiles activated by `VMConfig.allowTvmSolidity059()`), with an out-of-range ABI offset/length word. The attack is cheap for the attacker (one transaction, gas-limited by `getEnergyForData`, which is computed from `data.length`, not from the malicious length field, so the energy charged does not scale with the attempted allocation size). The likelihood of exploitability further depends on whether `allowTvmSelfdestructRestriction()` is active on the target network — I was not able to fully confirm from available context whether this proposal is currently activated on TRON mainnet by default, so the practical exposure on a fully up-to-date mainnet node is uncertain. On any network/version where that proposal has not yet been activated (e.g., private/test chains, or historical mainnet state before that proposal's activation height), the vulnerable code path is fully reachable.

### Recommendation
- Validate the extracted `len` in `extractBytes32Array`, `extractBytesArray`, and `extractSigArray` against the bounds of `words.length` (and a fixed `MAX_SIZE`) **before** allocating the array, independent of the `allowTvmSelfdestructRestriction()` feature flag.
- Move the existing `MAX_SIZE` check in `BatchValidateSign.doExecute` / `ValidateMultiSign.execute` outside the `if (VMConfig.allowTvmSelfdestructRestriction())` guard so it always applies.
- Ensure `getEnergyForData` energy cost also accounts for the declared array length, not only `data.length`, to prevent cheap DoS attempts even after the allocation is bounded.

### Proof of Concept
1. Craft calldata for `batchvalidatesign(bytes32,bytes[],address[])` where the ABI offset word for the `address[]` (or `bytes[]`) parameter points to a word whose value is set to `0x7FFFFFFF` instead of a real array length.
2. Deploy a trivial contract that forwards `CALL` to the `BatchValidateSign` precompile address with this crafted calldata.
3. Submit a transaction invoking that contract on a node where `allowTvmSelfdestructRestriction` is not yet enabled.
4. Observe `extractBytes32Array`/`extractBytesArray` attempting `new byte[0x7FFFFFFF][]`, causing significant GC pressure/OOM on the executing node — repeatable at low transaction cost since energy charged (`getEnergyForData`) is based on `data.length`, not the malicious length field.

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
