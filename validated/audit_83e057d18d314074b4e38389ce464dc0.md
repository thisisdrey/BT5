### Title
Unbounded array allocation from attacker-controlled ABI length before validation in `ValidateMultiSign`/`BatchValidateSign` precompiles - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
The `extractBytesArray`, `extractSigArray`, and `extractBytes32Array` helpers in `PrecompiledContracts.java` read an item count directly from attacker-supplied calldata and immediately allocate a Java array sized by that count, before validating that the count is consistent with the actual size of the supplied data or bounded by any sane maximum.

### Finding Description
`extractBytesArray` and `extractSigArray` read `len = words[offset].intValueSafe()` and then allocate `new byte[len][]` before any bound is checked against the real length of `data`/`words`: [1](#0-0)  and [2](#0-1) . `extractBytes32Array` has the identical pattern with no bound check at all: [3](#0-2) .

These helpers are invoked from the `ValidateMultiSign` (address `0x...a`) and `BatchValidateSign` (address `0x...9`) TVM precompiled contracts, which are reachable by any account issuing a `TriggerSmartContract` transaction that performs a `STATICCALL`/`CALL` to these fixed precompile addresses: [4](#0-3)  and [5](#0-4) .

Critically, the count-bound check (`sigArraySize > MAX_SIZE`, `addrArraySize > MAX_SIZE`) that would reject an oversized declared array length is only applied when `VMConfig.allowTvmSelfdestructRestriction()` is active: [6](#0-5)  and [7](#0-6) . This flag is a committee/chain-parameter-gated feature switch (default off until activated by proposal), toggled via `VMConfig.initAllowTvmSelfdestructRestriction` and read from `DynamicPropertiesStore`/`ProposalUtil` [8](#0-7) . When this proposal has not been activated on a given network, the legacy code path calls `extractBytesArray`/`extractSigArray`/`extractBytes32Array` directly with no size guard whatsoever, meaning the attacker-controlled `len` value (up to `Integer.MAX_VALUE` via `DataWord.intValueSafe()`) is used straight in `new byte[len][]`.

This exactly mirrors the CVE-2016-6307 bug class: a length field taken from untrusted input is used to allocate memory before the length is checked against the actual available/declared data, allowing a remote party to force excessive memory consumption.

### Impact Explanation
A single crafted `TriggerSmartContract` transaction whose EVM bytecode performs a `CALL`/`STATICCALL` to the `validateMultiSign` (0x...a) or `batchValidateSign` (0x...9) precompile addresses with calldata containing a huge value in the sig/address count word (interpreted via `words[...].intValueSafe()`) forces the node to attempt allocating a reference array with billions of entries (`new byte[len][]`), which is instantiated as an object-reference array (8 bytes/entry on 64-bit JVMs) independent of whether the backing calldata is actually that large. This can throw `OutOfMemoryError`/exhaust heap on the node processing the transaction (including during transaction execution while validating the block, i.e., every full node/witness executing this transaction), causing denial of service / node crash — matching the "no impact" exclusion criteria's allowed outcome of node crash or halt.

### Likelihood Explanation
Reaching this path requires only deploying/calling a smart contract that issues a `CALL` to the fixed precompile address with attacker-chosen calldata — no special privileges, keys, or SR/witness status are needed; any account able to broadcast a `TriggerSmartContract` transaction can reach it. The vulnerable legacy path is exercised only when the `allowTvmSelfdestructRestriction` chain parameter has not yet been activated on the network in question, which I could not directly confirm the current activation status of from the indexed code (the flag is toggled by governance proposal at runtime, not a compile-time constant), so this should be verified on the target deployment before treating it as immediately exploitable on any live/production java-tron network.

### Recommendation
Move the length/bound validation performed under the `allowTvmSelfdestructRestriction` flag so it is unconditional (not feature-gated), and additionally validate inside `extractBytesArray`, `extractSigArray`, and `extractBytes32Array` themselves that the declared `len` cannot exceed a reasonable bound (e.g., `MAX_SIZE`) and is consistent with `words.length` before any array is allocated, mirroring the existing `isValidAbiEncoding` structural check.

### Proof of Concept
Construct calldata for `batchvalidatesign(bytes32,bytes[],address[])` (or `validatemultisign`) where the ABI offset for the `bytes[]` array points to a word whose value (the declared array length) is set to a very large integer (e.g., `0x7fffffff`), while keeping the overall calldata short. Deploy a trivial contract that performs `staticcall` to precompile address `0x...9` (BatchValidateSign) with this calldata, and broadcast a `TriggerSmartContract` invoking it on a network where `allowTvmSelfdestructRestriction` is not yet active — `doExecute` calls `extractSigArray`/`extractBytesArray` with `len` unchecked, executing `new byte[len][]`/`new byte[len][32]`, which attempts a multi-gigabyte allocation and can throw `OutOfMemoryError`, disrupting the executing node.

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1057-1074)
```java
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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1162-1177)
```java
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

**File:** common/src/main/java/org/tron/core/vm/config/VMConfig.java (L195-197)
```java
  public static void initAllowTvmSelfdestructRestriction(long allow) {
    globalSnapshot.allowTvmSelfdestructRestriction = allow == 1;
  }
```
