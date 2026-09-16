### Title
Unvalidated attacker-controlled length field causes unbounded heap array allocation in `BatchValidateSign` precompile - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The reported CVE-2020-6150 is a heap overflow caused by trusting an attacker-controlled size field from a decompressed data section without validating it against the actual available buffer before allocating/copying. The same bug class — allocating/copying memory using a length value taken directly from untrusted input without a bounds check — exists in java-tron's `BatchValidateSign` TVM precompiled contract, reachable by any contract deployer/caller via a `CALL`/`STATICCALL` to the precompile address.

### Finding Description
`PrecompiledContracts.BatchValidateSign.doExecute` parses the precompile call data into `DataWord[] words` and then extracts a "signatures" array and an "addresses" array using length fields taken directly from the calldata: [1](#0-0) 

The size guard `sigArraySize > MAX_SIZE || addrArraySize > MAX_SIZE` is only applied **inside** the `if (VMConfig.allowTvmSelfdestructRestriction())` branch: [2](#0-1) 

`extractBytes32Array`, which builds the `addresses` array, is called **unconditionally**, outside of that guarded branch, and performs no validation whatsoever of `offset` or the length field it reads: [3](#0-2) 

`len` here is `words[offset].intValueSafe()`, a 32-byte word fully controlled by the calldata of the calling contract; `new byte[len][]` is executed before any range check against the actual data available, directly mirroring the CVE's pattern of allocating/reading a decompression buffer sized from an untrusted length field rather than the section's real bounds.

`extractBytesArray` (used for the "signatures" array when `allowTvmSelfdestructRestriction()` is disabled) has the same pattern — only an `offset > words.length - 1` check, no check on `len` before allocation: [4](#0-3) 

### Impact Explanation
An attacker who deploys or calls a smart contract that invokes the `BatchValidateSign` precompile can set the length word driving `extractBytes32Array` (and, when the `allowTvmSelfdestructRestriction` hard-fork flag is not active, `extractBytesArray`) to an extremely large value (e.g., close to `Integer.MAX_VALUE`). This forces the node executing/validating the transaction to attempt allocating a huge `byte[][]` reference array, causing severe heap pressure/`OutOfMemoryError` during block/transaction execution on every full node and SR that processes the block, i.e. a network-wide, single-transaction-triggered denial-of-service against transaction execution — the strongest realistic impact given the code path. The outer `execute()` method wraps `doExecute` in `catch (Throwable t)`, which does catch `OutOfMemoryError`, but the attempted allocation itself still stresses/exhausts heap for the executing thread's memory footprint and can degrade or destabilize node operation across all nodes replaying the same transaction.

### Likelihood Explanation
This path is reachable from any account by simply crafting the ABI-encoded input to `BatchValidateSign` and calling it from a deployed contract — no special privileges required. The `MAX_SIZE` guard only protects `addresses`/`signatures` extraction when `VMConfig.allowTvmSelfdestructRestriction()` is enabled, and `extractBytes32Array` is always unguarded, and additionally `extractBytesArray` is unguarded when that flag is disabled, so exploitability depends on the state of that hard-fork toggle. This limits certainty of live exploitability on current mainnet without confirming the flag's on-chain activation status.

### Recommendation
Move the `sigArraySize`/`addrArraySize` (and any length used by `extractBytes32Array`/`extractBytesArray`) validation against `MAX_SIZE` outside of the `allowTvmSelfdestructRestriction()` conditional so it is always enforced before any array is allocated, and additionally validate that `len` in `extractBytes32Array`/`extractBytesArray` cannot exceed the number of remaining `words` (i.e., bound it by `words.length - offset - 1`) before allocating, mirroring proper decompressed-section-length validation against actual buffer bounds.

### Proof of Concept
1. Deploy a contract that performs a low-level `staticcall`/`call` to the `BatchValidateSign` precompiled address.
2. Construct the call data so that `words[1]` (signature array offset) or `words[2]` (address array offset) points to a word whose value (`len`) is set to a very large integer (e.g. `0x7FFFFFFF`).
3. Send the transaction; when `doExecute` reaches `extractBytes32Array(words, offset)` (or `extractBytesArray` if the self-destruct-restriction flag is disabled), the node attempts `new byte[0x7FFFFFFF][]`, causing extreme memory pressure/`OutOfMemoryError` on every node that executes the transaction during block validation.

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1162-1181)
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
      int cnt = signatures.length;
      if (cnt == 0 || cnt > MAX_SIZE || signatures.length != addresses.length) {
        return Pair.of(true, DATA_FALSE);
      }
```
