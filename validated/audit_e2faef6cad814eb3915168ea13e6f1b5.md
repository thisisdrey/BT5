### Title
Unvalidated length field in `BatchValidateSign` precompile allows attacker-controlled unbounded array allocation - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
The `BatchValidateSign` TVM precompile parses signature/address counts directly from attacker-supplied calldata and, unless a specific hard-fork flag is active, passes those counts straight into array-allocation helper methods without any upper bound. This mirrors the reported bug class (an attacker-crafted length field used to allocate/copy memory without validating it against the available payload), and is reachable by any contract caller who can craft an EVM `CALL`/`STATICCALL` to the precompile address.

### Finding Description
`BatchValidateSign.doExecute` reads the raw ABI words of the call data and only bounds-checks the signature/address array sizes when `VMConfig.allowTvmSelfdestructRestriction()` is enabled: [1](#0-0) 

When that config flag is not active (which depends on chain hard-fork activation state), the code falls into `extractBytesArray(words, ..., data)` and `extractBytes32Array(words, ...)` with no size check at all. Both helpers take a length directly from the attacker-controlled word and use it to size a new array: [2](#0-1) 

`extractBytes32Array` computes `int len = words[offset].intValueSafe();` and then does `new byte[len][]`, iterating `len` times to fill it from further out-of-range words. An attacker can set this length near `Integer.MAX_VALUE`, forcing a huge array allocation (and subsequent large heap pressure) before any length-vs-payload validation occurs — the same root cause pattern as the Wireshark CVE-2023-0668 issue: a length field taken from untrusted input is used to size/allocate a buffer without first checking it against the size of the actual available data.

The exception thrown by `OutOfMemoryError` (or `ArrayIndexOutOfBoundsException` from `words[offset+i+1]`) is swallowed by the outer `catch (Throwable t)` in `execute`, so it does not directly crash the process, but the transient large allocation attempt still consumes heap and can degrade or destabilize the node under repeated invocation.

### Impact Explanation
Any account able to submit a `TriggerSmartContract` transaction (or deploy a contract that performs a raw `CALL` to the `BatchValidateSign` precompile address) can trigger repeated large-array allocation attempts, causing memory pressure, GC storms, or `OutOfMemoryError` conditions on validating/witness nodes that execute the transaction — a resource-exhaustion / potential node-crash vector reachable from an ordinary, unprivileged transaction.

### Likelihood Explanation
Exploitability depends on whether `VMConfig.allowTvmSelfdestructRestriction()` is active on the target network; on chains/hard-fork states where it is not yet enabled, the guard is entirely absent and any contract call can trigger the unbounded allocation with a single crafted `data` payload, making likelihood high in that configuration.

### Recommendation
Move the `sigArraySize`/`addrArraySize` (and equivalent) bound checks in `BatchValidateSign.doExecute` outside the `allowTvmSelfdestructRestriction()` gate so they always apply, and add explicit bounds validation inside `extractBytesArray`, `extractSigArray`, and `extractBytes32Array` verifying that the declared length is consistent with the actual size of `words`/`data` before allocating any array, similar to the `verifyLength` pattern used in `RLP.java`.

### Proof of Concept
Craft calldata for the `BatchValidateSign` precompile where the address-array offset word points to a length word set to a very large value (e.g., `0x7FFFFFFF`), and submit it via a contract `CALL`/`STATICCALL` to the precompile's fixed address on a node where `allowTvmSelfdestructRestriction()` is not yet enabled; `extractBytes32Array` will attempt `new byte[0x7FFFFFFF][]`, exhausting available heap on the executing node. [3](#0-2)

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
