### Title
Unbounded array allocation from attacker-controlled length word causes OOM in `BatchValidateSign`/`ValidateMultiSign` precompiles - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
`PrecompiledContracts.extractBytesArray`, `extractBytes32Array`, and `extractSigArray` read an array-length word directly out of caller-supplied calldata and immediately allocate a Java array of that size (`new byte[len][]`) before verifying that the declared length is consistent with the actual amount of data provided. This mirrors the CVE-2017-10800 pattern in GraphicsMagick's `ReadMATImage()`, where an object's declared size is trusted for allocation before validating it against the real payload size, leading to an out-of-memory condition.

### Finding Description
`extractBytes32Array` takes `len` straight from `words[offset].intValueSafe()` with no upper bound check against `words.length` before allocating: [1](#0-0) 

`extractBytesArray` has the same pattern — only checks that `offset` itself is within `words.length`, but not that `len` (the declared array length) is bounded by the actual data: [2](#0-1) 

`extractSigArray` follows the identical pattern: [3](#0-2) 

Both `ValidateMultiSign.execute` and `BatchValidateSign.doExecute` call these helpers using an ABI-supplied length word as `len`. Size validation against `MAX_SIZE` (5/16) is only applied when `VMConfig.allowTvmSelfdestructRestriction()` is enabled — the check that gates `extractBytes32Array` for the `addresses` array in `BatchValidateSign` and the switch between `extractSigArray`/`extractBytesArray` for `signatures`: [4](#0-3) 

When that hard-fork flag is not active (its default/pre-activation state), `extractBytesArray` and `extractBytes32Array` are invoked with a completely attacker-chosen `len`, which can be crafted up to `Integer.MAX_VALUE` via `intValueSafe()`. This immediately triggers a huge heap allocation (`new byte[len][]`) before any bounds check against the real calldata size — exactly the "size larger than actual data" OOM root cause described in the CVE.

### Impact Explanation
Any account can invoke a smart contract that performs a `CALL`/`STATICCALL` to the `batchvalidatesign` (`0x66`) or `validatemultisign` (`0x67`) precompiled contract addresses with a crafted length word. The resulting allocation attempt can throw an `OutOfMemoryError`, potentially destabilizing or crashing the node process handling the transaction, which is a denial-of-service against block-producing/validating full nodes — matching the "node crash or halt" acceptance criterion.

### Likelihood Explanation
The precompile is reachable by any unprivileged contract deployer/caller — no special permissions are required, only enough energy to reach the precompile dispatch (energy accounting for these calls is based on calldata length, not on the declared inner array length, so the cost of triggering the huge allocation is cheap relative to the potential OOM). The exposure specifically depends on whether `allowTvmSelfdestructRestriction` is active on the target network; on networks/forks where this proposal has not yet been activated (or where the guard is otherwise bypassed), the vulnerable path is directly reachable. I was not able to conclusively confirm from the indexed code whether this flag is enabled by default on current mainnet state (`DynamicPropertiesStore`'s default value could not be fully verified within tool limits), so likelihood should be validated against the target chain's actual proposal state before treating this as an immediately mainnet-exploitable issue.

### Recommendation
In `extractBytesArray`, `extractBytes32Array`, and `extractSigArray`, validate the declared `len` against a sane upper bound (e.g., the existing `MAX_SIZE` constants) and against the actual remaining `words.length`/`data.length` *before* allocating the array, regardless of the `allowTvmSelfdestructRestriction` flag state, rather than only when that hard-fork switch is active.

### Proof of Concept
1. Deploy a contract that performs `CALL` to precompiled address `0x0000...0066` (`batchValidateSign`) or `0x0000...0067` (`validateMultiSign`).
2. Craft calldata so that the ABI-encoded offset word for the `signatures`/`addresses` array points to a length word set to a very large value (close to `Integer.MAX_VALUE` after `intValueSafe()` truncation), while the rest of the calldata is short.
3. On a chain/fork where `allowTvmSelfdestructRestriction` is not activated, this reaches `extractBytesArray`/`extractBytes32Array` with the attacker-controlled `len`, triggering `new byte[len][]` and an `OutOfMemoryError` in the node processing the transaction. [5](#0-4)

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
