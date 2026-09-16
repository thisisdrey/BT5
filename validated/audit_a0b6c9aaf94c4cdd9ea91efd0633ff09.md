### Title
Unbounded array allocation from attacker-controlled ABI length in `BatchValidateSign` precompile causes node OOM crash - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The `BatchValidateSign` TVM precompiled contract decodes an attacker-supplied ABI array-length word directly into a Java array allocation (`new byte[len][]`) with no upper-bound check when the `allowTvmSelfdestructRestriction` chain parameter is not active for the calling code path. Any account able to send a transaction (or trigger a constant call) that invokes this precompile can supply a huge length value and force the executing node to attempt a multi-gigabyte array allocation, crashing or hanging the transaction-processing thread — the same "integer amplification → unbounded allocation" root cause as the Nuxt `v-for` island-rendering advisory (CVE-2026-71314).

### Finding Description
`BatchValidateSign.doExecute` parses the precompile's calldata into `DataWord[] words` and then extracts a signature array and an address array from attacker-controlled offsets: [1](#0-0) 

The `sigArraySize`/`addrArraySize` bound check (`> MAX_SIZE`, `MAX_SIZE = 16`) is only performed when `VMConfig.allowTvmSelfdestructRestriction()` is true: [2](#0-1) 

When that chain parameter is not yet activated, the code falls straight into `extractBytesArray` (for signatures) and unconditionally into `extractBytes32Array` (for addresses), neither of which performs any bound check: [3](#0-2) 

Both helpers read `len = words[offset].intValueSafe()` — a 32-byte word taken directly from the caller's calldata — and immediately allocate `new byte[len][]`. `intValueSafe()` clamps out-of-range/overflowing values to `Integer.MAX_VALUE` rather than producing a negative number, so an attacker can set this length word to a large positive value (e.g. `0x7fffffff`), causing the VM executing the transaction to attempt to allocate an array of over two billion object references. This is only checked for excessive size *after* the allocation already occurred, at line ~1179 (`cnt > MAX_SIZE`), which is too late: [4](#0-3) 

This is structurally identical to the Nuxt bug: a small, cheap request (here, a ~200-byte contract call) causes the server (here, the TVM executor) to expand an attacker-chosen integer into a proportional memory allocation with no cap enforced before the allocation happens.

### Impact Explanation
An `OutOfMemoryError` or extreme GC pressure triggered inside the energy-metered VM execution path is not guaranteed to be a simple, cleanly-caught exception — large array allocation failures can destabilize the JVM heap for the whole node process (not just the one transaction thread), because `OutOfMemoryError` can be thrown asynchronously in other threads sharing the heap. Even if it is caught locally, repeated submission of such transactions by an unprivileged broadcaster can be used to force repeated large allocations/GC pauses across the network as validators/witnesses execute the same transaction, degrading or halting block processing — this matches the “node crash or halt” / “API the node can no longer serve” impact bar.

### Likelihood Explanation
The precompile is reachable by any account that can call a smart contract (deploy a tiny contract calling the fixed precompile address, or use `TriggerConstantContract`/`TriggerSmartContract`), requiring no special privileges, no cooperation from other participants, and only whatever energy is available to a normal transaction sender. The vulnerable branch is gated purely on whether `allowTvmSelfdestructRestriction` has been activated via the on-chain proposal/parameter mechanism for the network in question; on any deployment where that specific proposal has not yet been voted in (private chains, testnets, or a mainnet prior to that specific hard-fork activation), the unguarded path is live by default.

### Recommendation
Move the `sigArraySize`/`addrArraySize` (and any other ABI-decoded array-length) validation in `BatchValidateSign.doExecute` outside the `allowTvmSelfdestructRestriction()` conditional so it is always enforced before calling `extractBytesArray`/`extractBytes32Array`/`extractSigArray`, and add an explicit upper-bound/sanity check (e.g., `len < 0 || len > MAX_SIZE`) inside `extractBytesArray` and `extractBytes32Array` themselves before performing `new byte[len][]`, independent of the calling feature flag.

### Proof of Concept
1. Deploy (or use `TriggerConstantContract`) a call to the `BatchValidateSign` precompile address on a network/version where `allowTvmSelfdestructRestriction` has not been activated.
2. Craft calldata such that `words[1]` (offset to the signature-array length word) points to a length word whose value is `0x7fffffff` (or any value that clamps to `Integer.MAX_VALUE` via `intValueSafe()`).
3. Send the transaction/call; `extractBytesArray` executes `int len = words[offset].intValueSafe();  byte[][] bytesArray = new byte[len][];` before any size check, forcing the node's VM executor to attempt a multi-gigabyte allocation from a single small transaction.

Note: I was unable to fully confirm, within the available tooling, whether `allowTvmSelfdestructRestriction` is currently activated by default on the mainnet chain parameters represented in this repository snapshot (the proposal-activation state is runtime/on-chain data, not static code), so the real-world exploitability depends on that activation status for the target deployment.

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1178-1181)
```java
      int cnt = signatures.length;
      if (cnt == 0 || cnt > MAX_SIZE || signatures.length != addresses.length) {
        return Pair.of(true, DATA_FALSE);
      }
```
