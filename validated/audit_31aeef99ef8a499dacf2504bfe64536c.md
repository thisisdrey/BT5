### Title
Unbounded array allocation in TVM precompiles `ValidateMultiSign`/`BatchValidateSign` causes attacker-triggered `OutOfMemoryError` at near-zero Energy cost - ([File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java])

### Summary
The `extractBytesArray`, `extractBytes32Array`, and `extractSigArray` helper methods in `PrecompiledContracts.java` allocate `new byte[len][]` where `len` is read directly from attacker-controlled call data via `words[offset].intValueSafe()`, with no upper bound check unless the `allowTvmSelfdestructRestriction` chain parameter has been activated. Any contract call (reachable from an unprivileged, unpermissioned transaction) to the fixed precompile addresses `0x9` (`BatchValidateSign`) or `0xa` (`ValidateMultiSign`) can supply a tiny-length call data buffer whose embedded "array length" word is set to a very large value (up to `Integer.MAX_VALUE`), forcing the node to attempt an allocation of a multi-gigabyte reference array while the Energy charged for the call is computed from the *physical* `data.length`, not from the attacker-controlled length field — decoupling cost from memory impact. [1](#0-0) 

### Finding Description
`extractBytesArray` (used by legacy `ValidateMultiSign`/`BatchValidateSign` execution) and `extractBytes32Array` allocate an array sized by an unchecked `intValueSafe()` value taken straight from the encoded call data: [2](#0-1) 

`extractSigArray` has the same pattern: [3](#0-2) 

These are invoked from `ValidateMultiSign.execute` and `BatchValidateSign.doExecute`. Both methods only guard the array size with `MAX_SIZE` when `VMConfig.allowTvmSelfdestructRestriction()` is enabled: [4](#0-3) [5](#0-4) 

When that governance-controlled parameter is *not* activated (which is the legacy/default path for most call flows, and the only path used when the hard-fork proposal has not been enabled on a given network), `extractBytesArray`/`extractBytes32Array`/`extractSigArray` execute with a completely unchecked `len`.

Critically, the Energy cost charged for these calls is computed purely from the physical byte length of the call data (`data.length`), independent of the embedded length word that drives the allocation: [6](#0-5) [7](#0-6) 

This means an attacker can craft a small call data buffer (a handful of 32-byte words, cheap in Energy) with one internal word set to a large value such as `0x7FFFFFFF`. When the precompile is reached, `new byte[0x7FFFFFFF][]` is attempted, which requires allocating an object-reference array of roughly 16 GB (8 bytes/ref × 2^31 entries on a 64-bit JVM) — an immediate `OutOfMemoryError` or a severe GC/memory-pressure event on the node processing the transaction. Because reaching these precompile addresses only requires a `CALL`/`STATICCALL` from any deployed contract (or a directly crafted TVM transaction), any unprivileged transaction broadcaster can trigger this repeatedly and cheaply.

### Impact Explanation
Triggering repeated large allocations across full nodes/SR nodes processing the same broadcast transaction can cause `OutOfMemoryError`s, forced GC pauses, and node instability — a network-wide denial-of-service that can degrade block production and halt transaction processing on affected nodes, mirroring the "memory consumption issue... remote attacker may be able to cause [resource exhaustion]" impact class in CVE-2020-3899. Because the same transaction is executed by every node that validates/replays the block (or during static/`eth_call`-style query paths), the DoS is easily amplified network-wide, not localized to a single node.

### Likelihood Explanation
The precompile addresses `0x9` and `0xa` are fixed, well-known TVM addresses; invoking them requires nothing more than deploying a trivial contract (or directly encoding a `TriggerSmartContract`) that performs a `CALL` to these addresses with attacker-crafted data. No special privileges, signatures, or asset ownership are required — only that the `allowTvmSelfdestructRestriction` parameter has not been activated on the target chain, which is the legacy/default behavior path exercised by `extractBytesArray`/`extractBytes32Array`.

### Recommendation
- Enforce an upper bound (e.g. the existing `MAX_SIZE` constants) on `len` in `extractBytesArray`, `extractBytes32Array`, and `extractSigArray` unconditionally, not only when `allowTvmSelfdestructRestriction` is active.
- Validate that `len` is consistent with the actual `data.length`/`words.length` before allocation (bounds already partially present in `extractSigArray`/`extractBytesArray` via the `offset > words.length - 1` check, but the `len` itself is never bounded against `words.length`).
- Tie the Energy cost computation to the attacker-supplied length fields (or reject if they disagree with `data.length`), so allocation cost cannot be decoupled from Energy charged.

### Proof of Concept
1. Encode a call data buffer for `validatemultisign(address,uint256,bytes32,bytes[])` (selector for address `0xa`) or `batchvalidatesign(bytes32,bytes[],address[])` (address `0x9`) with the minimum number of header words required to pass `data.length` checks.
2. Set the word at the offset that `extractBytesArray`/`extractSigArray` reads as the dynamic array length (`words[offset]`) to `0x7FFFFFFF` instead of a small legitimate array size, while keeping the physical call data itself small so `getEnergyForData` charges minimal Energy.
3. Deploy a helper contract that performs a `STATICCALL`/`CALL` to precompile address `0x9` or `0xa` with this crafted data, and broadcast a `TriggerSmartContract` transaction invoking it (only relevant when `allowTvmSelfdestructRestriction` has not been activated on the target network, so the legacy `extractBytesArray`/`extractBytes32Array`/`extractSigArray` path executes without the `MAX_SIZE` guard).
4. On execution, `new byte[0x7FFFFFFF][]` is attempted inside `extractBytesArray`/`extractBytes32Array`/`extractSigArray`, causing an immediate `OutOfMemoryError` (or severe memory pressure) on every node that executes/validates the transaction, for negligible Energy cost.

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1044-1049)
```java
    @Override
    public long getEnergyForData(byte[] data) {
      long cnt = (data.length / WORD_SIZE - 5) / 5;
      // one sign 1500, half of ecrecover
      return cnt * ENGERYPERSIGN;
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1066-1074)
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
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1137-1142)
```java
    @Override
    public long getEnergyForData(byte[] data) {
      long cnt = (data.length / WORD_SIZE - 5) / 6;
      // one sign 1500, half of ecrecover
      return cnt * ENGERYPERSIGN;
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
