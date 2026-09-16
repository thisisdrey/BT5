### Title
Unbounded array allocation from attacker-controlled length word in precompiled contract signature parsing - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
CVE-2017-6436 is a memory-allocation DoS caused by `parse_string_node` in libplist trusting a length field from a crafted file to size an allocation without validating it against the actual available data. The java-tron analog is the helper functions `extractBytes32Array`, `extractBytesArray`, and `extractSigArray` in `PrecompiledContracts.java`, which read an array-length word directly from attacker-supplied TVM calldata and use it to size a `byte[][]` allocation before any bound check is guaranteed to run.

### Finding Description
`extractBytes32Array` reads the count straight from calldata and allocates an array of that size with no upper bound at all: [1](#0-0) 

`extractBytesArray` and `extractSigArray` have the same pattern — the `len` value comes from `words[offset].intValueSafe()` (an attacker-controlled ABI word) and is used directly to allocate `new byte[len][]` before iterating: [2](#0-1) [3](#0-2) 

These helpers are invoked from two reachable precompiled contracts:

1. `ValidateMultiSign` (`ValidateMultiSign.execute`), where the size guard (`sigArraySize > MAX_SIZE`) is only applied when `VMConfig.allowTvmSelfdestructRestriction()` is enabled, and the unbounded `extractBytesArray`/`extractSigArray` call happens outside any try/catch in this method: [4](#0-3) 

2. `BatchValidateSign.doExecute`, same conditional-guard pattern for both the signature array and the address array (`extractBytes32Array`), again only bounded when the flag is on: [5](#0-4) 

When `VMConfig.allowTvmSelfdestructRestriction()` is disabled (a config-dependent hard-fork switch), an unprivileged contract caller can craft calldata where the length word at the referenced offset is an arbitrarily large 32-bit value (e.g. `0x7FFFFFFF`), causing `new byte[len][]` to attempt to allocate up to ~2^31 array-reference slots, which throws `OutOfMemoryError`.

### Impact Explanation
An `OutOfMemoryError` thrown during transaction/contract execution can destabilize the JVM heap for the whole node process, not just the single transaction, since Java `OutOfMemoryError` is frequently non-recoverable and can leave other threads (block application, RPC, P2P) in inconsistent states or cause cascading failures — this maps to the "node crash or halt" acceptance criterion. In `ValidateMultiSign`, the vulnerable call sits outside the method's try/catch block, so it is not gracefully absorbed the way exceptions inside the try-block are.

### Likelihood Explanation
The precompiled contracts are reachable via a plain `CALL` from any smart contract to a fixed precompile address with crafted calldata — no special privilege is required, only enough energy to reach the call (the energy cost model for these calls is based on data length, not the value encoded inside a length word, so a small calldata payload can still encode a huge count field). Exploitability is gated by the `allowTvmSelfdestructRestriction()` hard-fork flag being disabled; I was not able to confirm the current default/activation state of this flag on mainnet within the available context, which is a source of uncertainty for real-world exploitability today.

### Recommendation
Add an explicit upper-bound check on `len` in `extractBytes32Array`, `extractBytesArray`, and `extractSigArray` unconditionally (not gated behind `VMConfig.allowTvmSelfdestructRestriction()`), and validate `len` against the remaining `words.length` before allocating, consistent with the pattern used for `verifyLength` in `RLP.java`: [6](#0-5) 

### Proof of Concept
Deploy a contract that performs a raw `CALL` (or use `staticcall`) to the `BatchValidateSign` / `ValidateMultiSign` precompile address with calldata whose word at the address-array/signature-array offset field is set to `0x7FFFFFFF` (or another very large 32-bit value) instead of a legitimate small count, while `VMConfig.allowTvmSelfdestructRestriction()` is not yet active. `extractBytes32Array`/`extractBytesArray` will attempt `new byte[2147483647][]`, triggering `OutOfMemoryError` during execution.

**Uncertainty note:** I could not verify from the indexed context whether `VMConfig.allowTvmSelfdestructRestriction()` is currently enabled by default in the target deployment, nor confirm whether an outer top-level `Throwable`/`OutOfMemoryError` catch exists further up the TVM call stack that would fully contain this. Confirming these two points would require running the code or checking the live chain parameter, which is outside what the indexed context can provide.

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
