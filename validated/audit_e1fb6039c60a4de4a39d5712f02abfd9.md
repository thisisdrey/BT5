### Title
Unchecked attacker-controlled array-length count used for allocation before bounds validation in `ValidateMultiSign`/`BatchValidateSign` precompile helpers - ([File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java])

### Summary
The `extractBytesArray` and `extractBytes32Array` helper methods in `PrecompiledContracts.java`, used by the `ValidateMultiSign` and `BatchValidateSign` TVM precompiled contracts, read a length/count value directly out of attacker-controlled calldata and immediately allocate a Java array of that size, before any bound check is performed against the actual data available. This mirrors the `russh` CVE-2026-48107 pattern: `Vec::with_capacity(n_prompts)` on an unvalidated, attacker-supplied count taken from a message before checking it against the remaining packet size.

### Finding Description
`extractBytesArray` and `extractBytes32Array` read `len` from `words[offset].intValueSafe()` and allocate immediately: [1](#0-0) 

Both helpers are invoked from `ValidateMultiSign.execute()` and `BatchValidateSign.execute()`, which are TVM precompiled contracts reachable by any contract that issues a `CALL`/`STATICCALL` to the fixed precompile address (`validateMultiSignAddr` / `batchValidateSignAddr`) — i.e., reachable by any unprivileged contract deployer/caller once `VMConfig.allowTvmSolidity059()` is active: [2](#0-1) 

In `ValidateMultiSign.execute`, the bound check on the signature-array size (`sigArraySize > MAX_SIZE`) is only performed when the `allowTvmSelfdestructRestriction` feature flag is enabled, and even then it only gates the `extractSigArray` path. When that flag is not enabled, `extractBytesArray` is called with **no prior size check at all**, and the `signatures.length > MAX_SIZE` validation only happens *after* the array has already been allocated inside `extractBytesArray`: [3](#0-2) 

Similarly, `BatchValidateSign.doExecute` only checks `sigArraySize`/`addrArraySize` against `MAX_SIZE` when `allowTvmSelfdestructRestriction()` is true; the `extractBytesArray`/`extractBytes32Array` calls that actually perform the unchecked allocation are unconditional, and the `cnt > MAX_SIZE` check happens only after both arrays have already been allocated: [4](#0-3) 

Because `allowTvmSelfdestructRestriction` is a runtime/chain-parameter feature switch (default `false` until a committee proposal activates it) as shown in `VMConfig`, any network or period where it is not yet activated exposes the unguarded path: [5](#0-4) 

The `len` value comes from `DataWord.intValueSafe()` on attacker-supplied calldata (a 256-bit ABI word interpreted as an offset/length), which can be made arbitrarily large (up to `Integer.MAX_VALUE`). `new byte[len][]` is then attempted, which for a large `len` either throws `OutOfMemoryError`/`NegativeArraySizeException` or forces a very large heap allocation before the loop even tries to read `words[offset + i + 1]` (which would itself throw `ArrayIndexOutOfBoundsException` once `i` exceeds the actual `words` array length) — exactly the "count read before validating remaining data" defect described in the `russh` advisory.

### Impact Explanation
A malicious contract caller (any unprivileged account that can send a transaction calling into a contract that itself calls `validatemultisign`/`batchvalidatesign`, or directly crafts calldata hitting the precompile) can trigger an oversized array allocation attempt on every full node that executes the transaction (all nodes that must re-execute transactions to reach consensus). This can cause `OutOfMemoryError` inside the TVM execution path, potentially destabilizing or crashing the node process, which given java-tron's use of `Throwable`/`OutOfTimeException` handling around some of these precompiles is not guaranteed to be fully contained (the exception path is only explicitly caught in `BatchValidateSign.execute`'s outer try/catch, not inside `ValidateMultiSign.execute`, whose catch block only catches `Throwable t` after signature recovery, not around the initial `extractBytesArray` call at line 1074). This matches the "node crash or halt" acceptance criterion.

### Likelihood Explanation
Reachability requires only a single crafted TVM contract call, no special privileges, and is exploitable by any account that can broadcast a transaction. The gating condition is that `allowTvmSelfdestructRestriction` has not been activated for the code path taken by `ValidateMultiSign`/`BatchValidateSign`; on chains/testnets where the flag is not yet on, this is directly reachable. This reduces confidence to "reachable under specific but realistic operational conditions" rather than "always reachable on every network state."

### Recommendation
In `extractBytesArray`, `extractBytes32Array`, and `extractSigArray`, validate the decoded `len` against a maximum bound (e.g., `MAX_SIZE`) and against the actual remaining length of the `words` array *before* allocating `new byte[len][]`, mirroring the fix pattern in the `russh` advisory (validate count against available data before `Vec::with_capacity`/array allocation). Make this check unconditional (not gated behind `allowTvmSelfdestructRestriction`) so both legacy and new code paths are protected.

### Proof of Concept
Craft calldata for `validatemultisign(address,uint256,bytes32,bytes[])` (or `batchvalidatesign`) where the ABI-encoded `bytes[]` length word (read via `words[offset].intValueSafe()`) is set to a very large value (e.g., `0xFFFFFFFF`) while the actual calldata is short. With `allowTvmSelfdestructRestriction` not activated, `ValidateMultiSign.execute` calls `extractBytesArray(words, offset, rawData)` directly at: [6](#0-5) 
which executes `new byte[len][]` at line 404 before any bound check, attempting a massive allocation on the executing node.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L254-259)
```java
    if (VMConfig.allowTvmSolidity059() && address.equals(batchValidateSignAddr)) {
      return batchValidateSign;
    }
    if (VMConfig.allowTvmSolidity059() && address.equals(validateMultiSignAddr)) {
      return validateMultiSign;
    }
```

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

**File:** common/src/main/java/org/tron/core/vm/config/VMConfig.java (L303-305)
```java
  public static boolean allowTvmSelfdestructRestriction() {
    return current().allowTvmSelfdestructRestriction;
  }
```
