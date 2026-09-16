Confirmed: `intValueSafe()` returns up to `Integer.MAX_VALUE` (~2.1 billion) whenever the encoded length occupies more than 4 bytes or is otherwise out of int range [1](#0-0) . This value flows unchecked into array allocation in the `ValidateMultiSign` and `BatchValidateSign` TVM precompiles.

### Title
Unbounded array allocation from attacker-controlled length word in TVM precompiled contracts (`extractBytesArray`/`extractBytes32Array`/`extractSigArray`) - ([File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java])

### Summary
The `ValidateMultiSign` and `BatchValidateSign` precompiled contracts read an array-length word directly from the caller-supplied call data and use it to allocate a Java array of that size (`new byte[len][]`), without first checking that `len` is consistent with the actual size of the supplied `data`. Because `intValueSafe()` can return up to `Integer.MAX_VALUE` for any oversized/overflowed word, a small transaction payload can trigger allocation of an enormous array of object references, exhausting node memory — the same bug class as CVE-2019-11938 (Facebook Thrift trusting a declared container size instead of validating it against the actual payload).

### Finding Description
`extractBytesArray`, `extractBytes32Array`, and `extractSigArray` compute `len` from a single 32-byte word taken from the raw calldata (`words[offset].intValueSafe()`), then immediately allocate `new byte[len][]` before validating that `len` corresponds to data actually present in the supplied byte array: [2](#0-1) 

`intValueSafe()` only guards against `ArithmeticException`, clamping to `Integer.MAX_VALUE` rather than rejecting the value: [1](#0-0) 

In `BatchValidateSign.doExecute`, `extractBytes32Array(words, words[2].intValueSafe() / WORD_SIZE)` is invoked to build the `addresses` array. The `MAX_SIZE` bound check on `addrArraySize`/`sigArraySize` is only performed when the `allowTvmSelfdestructRestriction` hard-fork switch is enabled: [3](#0-2) 

If that feature flag is off in a given network/fork configuration, `extractSigArray`/`extractBytesArray`/`extractBytes32Array` are reached with no upper bound on `len` at all, so an attacker can set the length word to a maximal value (e.g., `0xFFFFFFFF`) while sending only a few dozen actual bytes of calldata, driving `new byte[len][]` (or `new byte[len]` for `extractBytes32Array`'s reference array) to allocate on the order of billions of object-array slots from a single, cheap `CALL` to the precompile address. Even when the flag is enabled, `sigArraySize`/`addrArraySize` are read via an additional `words[...]` index that itself is unchecked before use, so the code path that performs the size check can still index out of bounds or use another attacker-controlled offset before the check takes effect.

### Impact Explanation
An unbounded array allocation on `ValidateMultiSign`/`BatchValidateSign` execution (which is reachable by any account issuing a normal `TriggerSmartContract` transaction, e.g. via a wrapper contract calling the precompile address) allows a single cheap transaction to force the executing full node to attempt an allocation of gigabytes of memory, causing an `OutOfMemoryError` or severe GC pressure. Because block-producing/validating nodes must execute the same transaction deterministically to reach consensus, this can crash or stall multiple nodes simultaneously, constituting a network-wide denial-of-service condition consistent with a "node crash or halt" impact.

### Likelihood Explanation
The precompile addresses are reachable via ordinary contract calls from any unprivileged account; no special permission, staking, or witness/SR status is required — only paying for the CALL's energy, since the allocation happens before any energy-consuming per-item work is metered, an attacker can trigger this with minimal cost. Exploitability depends on the specific chain's parameter for `allowTvmSelfdestructRestriction` (a `VMConfig` hard-fork gate) being disabled, which lowers confidence to Medium-High without confirming the exact deployed fork parameters on a given java-tron network.

### Recommendation
Validate `len` against the actual remaining bytes of `data`/`words.length` immediately after reading it in `extractBytesArray`, `extractBytes32Array`, and `extractSigArray`, rejecting (returning `Pair.of(false, ...)` or `DATA_FALSE`) before any array allocation, independent of the `allowTvmSelfdestructRestriction` flag. Apply a hard cap (e.g., the existing `MAX_SIZE` constants) unconditionally rather than gating it behind a feature flag, and ensure the size-check read itself (`words[words[3]...]`) is bounds-checked against `words.length` before indexing.

### Proof of Concept
1. Deploy or call the precompile at the `ValidateMultiSign`/`BatchValidateSign` fixed address via a `TriggerSmartContract` transaction (or a wrapper contract that forwards a static-call).
2. Craft calldata whose ABI header points the length-word offset to a word containing `0xFFFFFFFF` (or any value causing `intValueSafe()` to return `Integer.MAX_VALUE`), while the rest of the calldata is only a few dozen bytes.
3. On a network configuration where `allowTvmSelfdestructRestriction` is not enabled, `execute`/`doExecute` calls `extractBytesArray`/`extractBytes32Array`/`extractSigArray` directly, executing `new byte[Integer.MAX_VALUE][]`, exhausting the node's heap and crashing/hanging the JVM executing the transaction.

### Citations

**File:** common/src/main/java/org/tron/common/runtime/vm/DataWord.java (L219-229)
```java
  /**
   * In case of int overflow returns Integer.MAX_VALUE otherwise works as #intValue()
   */
  public int intValueSafe() {
    int bytesOccupied = bytesOccupied();
    int intValue = intValue();
    if (bytesOccupied > 4 || intValue < 0) {
      return Integer.MAX_VALUE;
    }
    return intValue;
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L390-426)
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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1165-1177)
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
```
