## Analog Found

### Title
Unbounded array allocation from attacker-controlled length word in `ValidateMultiSign`/`BatchValidateSign` precompiles - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The Pillow BLP CVE (`ALPINE-CVE-2021-27921`) is a DoS caused by trusting a size field embedded in untrusted input to drive a memory allocation before the value is validated against the actual data available. The same bug class exists in java-tron's `ValidateMultiSign` and `BatchValidateSign` TVM precompiled contracts (addresses `0x0a` and `0x09`), where a 32-byte "array length" word taken directly from EVM calldata is used to size a `byte[][]` allocation before any bound check is applied, when the `allowTvmSelfdestructRestriction` chain feature is not active.

### Finding Description
`ValidateMultiSign.execute()` and `BatchValidateSign.doExecute()` parse the raw call data into `DataWord[] words` and then compute the signature-array length: [1](#0-0) 

Only when `VMConfig.allowTvmSelfdestructRestriction()` is true is `sigArraySize` checked against `MAX_SIZE` *before* extraction. When that flag is not active, `extractBytesArray` is called directly with no upper bound on the decoded length: [2](#0-1) 

Inside `extractBytesArray`, `len` is read from a single attacker-controlled `DataWord` via `intValueSafe()` and immediately used to allocate a reference array with `new byte[len][]`, with the `signatures.length > MAX_SIZE` bound only enforced *after* the allocation has already happened: [3](#0-2) 

The same unchecked-length-before-allocation pattern also exists in `extractBytes32Array`, used identically by `BatchValidateSign`: [4](#0-3) 

Critically, the energy cost charged for calling these precompiles is derived only from `data.length` (the actual calldata size), not from the decoded length word: [5](#0-4) 

This means a caller can submit small, cheap calldata (a handful of 32-byte words) while embedding a huge value (up to `Integer.MAX_VALUE`) in the word that is read as the array length, causing `new byte[len][]` to attempt to allocate an enormous reference array (tens of GB) for a trivial energy cost — the same "declared container size trusted for allocation without validating against actual available data" flaw as the Pillow BLP CVE.

### Impact Explanation
A successful trigger causes a large/failing heap allocation inside precompile execution on a full node, which can throw `OutOfMemoryError` or place severe GC pressure on the JVM, degrading or crashing block-processing/transaction-execution — a resource-exhaustion/node-crash impact reachable from a single externally-owned account's smart-contract call (`TriggerSmartContract`) to a `CALL`/`STATICCALL` targeting precompile address `0x09` or `0x0a`.

### Likelihood Explanation
This is **conditional on chain configuration**: the bound check that prevents the unsafe allocation is gated by `VMConfig.allowTvmSelfdestructRestriction()`, a governance-activated feature flag. On networks/chain states where this proposal has already been activated (which is very likely the case on long-running TRON mainnet, since this is an older maintenance-era feature), the guarded `extractSigArray`/bounded path is used and the length is checked against `MAX_SIZE` (5 or 16) before allocation, closing this specific hole. On any chain state, private network, or historical replay where the proposal has not been activated, the legacy unguarded `extractBytesArray`/`extractBytes32Array` path is fully live and directly reachable from any unprivileged transaction. I could not verify from the repo alone whether this proposal is unconditionally hardcoded to "active" for all networks or remains a toggleable `DynamicPropertiesStore` parameter with a real "off" state at genesis — this is the key uncertainty in this finding.

### Recommendation
Move the `sigArraySize`/`addrArraySize` bound checks so they are unconditional (not gated behind `allowTvmSelfdestructRestriction()`), and validate the decoded length against both `MAX_SIZE` and the remaining `words.length` *before* any `new byte[len][]` allocation in `extractBytes32Array`, `extractBytesArray`, and `extractSigArray`.

### Proof of Concept
Craft calldata for `validateMultiSign(address,uint256,bytes32,bytes[])`-style layout where the offset word (`words[3]`) points to a length word set to `0x7FFFFFFF` (or any huge value), while keeping the actual byte array short. On a chain where `allowTvmSelfdestructRestriction` is not active, invoking this via a contract `CALL` to precompile address `0x0a` reaches `extractBytesArray`, which executes `new byte[2147483647][]` before the `signatures.length > MAX_SIZE` check is ever reached, for the cost of only a few words' worth of energy.

**Caveat**: I could not confirm from the indexed repo content whether `allowTvmSelfdestructRestriction` is unconditionally true on the live TRON mainnet today or remains a genuine on/off `DynamicPropertiesStore` parameter; if the guarding proposal is permanently active on mainnet, this specific reachable path is already closed there, and only the "codebase still contains an unguarded, gate-dependent allocation" concern remains valid. If a definitive answer on current mainnet parameter state is required, a Devin session with access to running node/chain data would be needed to check the live `DynamicPropertiesStore` value for this parameter.

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1044-1049)
```java
    @Override
    public long getEnergyForData(byte[] data) {
      long cnt = (data.length / WORD_SIZE - 5) / 5;
      // one sign 1500, half of ecrecover
      return cnt * ENGERYPERSIGN;
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1066-1078)
```java
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
