## Title
Unbounded Attacker-Controlled Array Length in TVM Precompile ABI Decoding Causes Node OOM/DoS - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The `ValidateMultiSign` and `BatchValidateSign` precompiled contracts (TVM addresses `0x...0a` and `0x...09`) decode a "dynamic array length" word directly out of attacker-supplied calldata and use it, unchecked, as the size of a Java array allocation before any bound/plausibility check is performed. This mirrors the jsPDF advisory's root cause (CWE-20/CWE-835): a value taken from untrusted input is used to drive resource-consuming work before validation, allowing a cheap, malformed input to consume disproportionate CPU/memory and cause denial of service.

### Finding Description
`extractBytesArray` and `extractBytes32Array` read the array length straight from the caller-controlled `DataWord[]` without capping it against the real size of the input buffer: [1](#0-0) 

`intValueSafe()` only clamps to `Integer.MAX_VALUE`, it does not validate the value against the actual calldata length: [2](#0-1) 

These helpers are invoked from `ValidateMultiSign.execute` and `BatchValidateSign.doExecute`, both of which are reachable by any unprivileged contract call to the corresponding precompile address once `allowTvmSolidity059()` is enabled: [3](#0-2) [4](#0-3) 

Critically, the `extractBytesArray`/`extractBytes32Array` (legacy, non-bounded) code path is only guarded by the runtime committee flag `VMConfig.allowTvmSelfdestructRestriction()`. When that flag is *not* active, neither the `sigArraySize`/`addrArraySize` bound check nor the safer `extractSigArray` (whose length is still separately unclamped for `extractBytes32Array`) applies, and `new byte[len][]` / `bytes32Array = new byte[len][]` can be requested with `len` up to `Integer.MAX_VALUE`, causing a huge/OOM-inducing allocation from a tiny transaction payload — directly analogous to the jsPDF bug where an attacker-controlled length field drives unbounded work before the code validates the input is well-formed.

### Impact Explanation
A single crafted transaction that calls the `validatemultisign`/`batchvalidatesign` precompile with a small calldata buffer, but with the "array length" word set to a large value (e.g., `0x7fffffff`), forces the node to attempt allocating an object array of that size. This can trigger `OutOfMemoryError`/severe GC pressure across the JVM process executing the transaction (including full nodes validating/re-executing the block), which is a node-crash/halt class impact — matching the "node crash or halt" acceptance criterion.

### Likelihood Explanation
The precompiles are reachable from any smart-contract `CALL`/`DELEGATECALL` to a fixed, well-known address once the underlying TIP (`allowTvmSolidity059`) is active, which requires no special privilege — any contract deployer/caller can trigger it. The only gating condition is that the newer `allowTvmSelfdestructRestriction` committee flag has not yet been activated on the target chain (relevant to private/consortium/test chains, or any network state where that specific proposal hasn't been voted in), which the code cannot assume is universally true — it is a runtime, not compile-time, condition, so the vulnerable path exists in the shipped code.

### Recommendation
- In `extractBytesArray` and `extractBytes32Array`, validate `len` against the number of remaining `words` (or the effective calldata size) before allocating, independent of the `allowTvmSelfdestructRestriction` flag.
- Apply the `MAX_SIZE` bound check unconditionally (not only when the restriction flag is enabled) prior to calling either extraction helper.
- Consider charging/estimating energy proportional to the decoded length before performing any allocation.

### Proof of Concept
1. Deploy any contract that performs `staticcall`/`call` to precompile address `0x...0a` (`validateMultiSign`) on a java-tron node/chain where `allowTvmSolidity059` is enabled but `allowTvmSelfdestructRestriction` has not been activated.
2. Craft calldata that satisfies the minimal ABI header (5 words) but sets the word at the dynamic "signatures" array offset to `0x7fffffff` as its declared length, with insufficient trailing data.
3. Invoke the contract; `extractBytesArray`/`extractSigArray`-equivalent path executes `new byte[len][]` with `len = Integer.MAX_VALUE`, causing large memory allocation attempts and JVM-wide resource exhaustion on every node re-executing the transaction.

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1066-1075)
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
