### Title
Unbounded array allocation in TVM signature-verification precompiles (`ValidateMultiSign`/`BatchValidateSign`) allows attacker-controlled OOM/DoS via a tiny transaction - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
`PrecompiledContracts.extractBytesArray`, `extractSigArray`, and `extractBytes32Array` read an array-length field directly out of ABI-encoded calldata and immediately allocate `new byte[len][]` before validating that `len` is consistent with the actual amount of data present or with the precompile's `MAX_SIZE` cap. This is the same bug class as the reported Netty `Lz4FrameDecoder` issue: a small, cheap message carries an untrusted size field that is trusted to size a large allocation before the payload backing that size is verified.

### Finding Description
`extractBytes32Array` and `extractBytesArray` take the element count straight from calldata with no upper bound and no check against the remaining `words` length before allocating: [1](#0-0) 

`ValidateMultiSign.execute` and `BatchValidateSign.doExecute` only enforce the `MAX_SIZE` bound on the array length *before* calling these extractors when `VMConfig.allowTvmSelfdestructRestriction()` is enabled; on the legacy/disabled path, `extractBytesArray`/`extractSigArray` (and, in `BatchValidateSign`, `extractBytes32Array` for the address array) are invoked directly on the attacker-supplied length with no size check at all: [2](#0-1) [3](#0-2) 

The length-bound check on `signatures.length`/`cnt` that does exist happens only *after* the extraction call has already returned — i.e., after the oversized array has already been allocated: [4](#0-3) [5](#0-4) 

By contrast, the RLP decoder in the same codebase treats this exact scenario as a security-sensitive checkpoint, calling `verifyLength(suppliedLength, availableLength)` before trusting any header-derived length: [6](#0-5) 

No equivalent bounds check exists in `extractBytes32Array`/`extractBytesArray`/`extractSigArray` for the unrestricted code path — the `len` value (`words[offset].intValueSafe()`) can be set by the caller to a large positive `int` (bounded only by `intValueSafe()`'s clamp to the `int` range, i.e. up to `Integer.MAX_VALUE`), and `new byte[len][]` is attempted immediately.

### Impact Explanation
`new byte[Integer.MAX_VALUE][]` (or any multi-hundred-million-element value) attempts to allocate on the order of many gigabytes for the reference array alone (8 bytes per reference on a 64-bit JVM), which will throw `OutOfMemoryError` or place severe GC pressure on the node executing the transaction. Because this occurs inside contract execution triggered by any incoming transaction (or even validation of an unconfirmed/pending transaction during energy estimation / `TriggerConstantContract`), a single crafted call can degrade or crash a full node — a CWE-400/770 resource-exhaustion condition matching the "node crash or halt" acceptance criterion. Both `ValidateMultiSign` (address `...0x66` style TVM precompile) and `BatchValidateSign` are reachable by any unprivileged account issuing a `TriggerSmartContract`/CALL to the precompile address, and via any deployed contract that calls them internally (e.g., multisig wallets).

### Likelihood Explanation
Exploitability depends on the `allowTvmSelfdestructRestriction` feature flag: when enabled (the newer, hardened code path), the `MAX_SIZE` check happens before the unbounded allocation for `sigArraySize`/`addrArraySize`, closing this specific gap. When the flag is not active — e.g., on chains/testnets/private forks of this codebase that have not activated that proposal, or in `ValidateMultiSign`'s pre-check for its own `sigArraySize` gate which is likewise conditional on the same flag — the extractors are called with the raw attacker length and no upper bound at all. The calldata required to trigger this is tiny (a handful of 32-byte words), and the energy cost charged (`getEnergyForData`) is computed purely from `data.length` (the small calldata size), not from the attacker-chosen `len` value, so the fee paid is disproportionate to the resource consumption triggered — mirroring the Netty PoC's "22 bytes forces 32MB allocation" pattern.

### Recommendation
In `extractBytes32Array`, `extractBytesArray`, and `extractSigArray`, validate the decoded `len` against both a hard maximum (e.g., the precompile's `MAX_SIZE`) and against the number of remaining `words`/available calldata *before* allocating the `byte[len][]` array, unconditionally (not gated behind `VMConfig.allowTvmSelfdestructRestriction()`). This mirrors the `verifyLength` pattern already used in `RLP.java` and ensures the length-vs-available-data check always precedes allocation regardless of feature-flag state.

### Proof of Concept
1. Craft calldata for `batchvalidatesign(bytes32,bytes[],address[])` (or `validatemultisign`) where the ABI offset/length word for the `address[]` (or `bytes[]`) parameter is set to a very large value, e.g. `0x7FFFFFFF`, while the surrounding calldata is only a few words long.
2. Submit this as calldata to a `TriggerSmartContract` (or `TriggerConstantContract`) call targeting the `BatchValidateSign`/`ValidateMultiSign` precompiled contract address, on a network configuration where `VMConfig.allowTvmSelfdestructRestriction()` is false.
3. Execution reaches `extractBytes32Array(words, offset)` / `extractBytesArray(...)`, which executes `int len = words[offset].intValueSafe(); byte[][] arr = new byte[len][];` with `len` ≈ `0x7FFFFFFF`, causing the JVM to attempt a multi-gigabyte allocation and throw `OutOfMemoryError`, well before the `MAX_SIZE`/`signatures.length` bound checks that occur later in the method.

Note: I was not able to fully verify from the indexed code whether `allowTvmSelfdestructRestriction` is unconditionally active on current mainnet (its state is read from `DynamicPropertiesStore`); this determines whether the unguarded legacy path is reachable on production mainnet versus only on chains/forks where that TIP has not been activated.

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1064-1078)
```java
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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1165-1181)
```java
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

**File:** framework/src/main/java/org/tron/core/capsule/utils/RLP.java (L605-617)
```java
  /**
   * Compares supplied length information with maximum possible
   *
   * @param suppliedLength Length info from header
   * @param availableLength Length of remaining object
   * @throws RuntimeException if supplied length is bigger than available
   */
  private static void verifyLength(int suppliedLength, int availableLength) {
    if (suppliedLength > availableLength) {
      throw new RuntimeException(String.format("Length parsed from RLP (%s bytes) is greater "
          + "than possible size of data (%s bytes)", suppliedLength, availableLength));
    }
  }
```
