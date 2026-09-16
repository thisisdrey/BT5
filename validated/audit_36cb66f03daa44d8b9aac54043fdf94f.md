### Title
Unbounded array allocation in `ValidateMultiSign` precompile before size check - ([File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java])

### Summary
The `ValidateMultiSign` TVM precompiled contract (address `0x0...09`, reachable from any smart-contract call made by a signed transaction) parses an attacker-controlled signature-array length from call data and allocates a Java array sized by that value *before* it is bounds-checked against `MAX_SIZE`, when the `allowTvmSelfdestructRestriction` hard-fork flag is not active for the executing snapshot.

### Finding Description
In `PrecompiledContracts.ValidateMultiSign.execute()`: [1](#0-0) 

the sig-count bound check (`sigArraySize > MAX_SIZE`) is only performed when `VMConfig.allowTvmSelfdestructRestriction()` is `true`. When that flag is `false`, `extractBytesArray()` is invoked directly with no prior bound check: [2](#0-1) 

Inside `extractBytesArray`, `len` is read via `words[offset].intValueSafe()` — `intValueSafe()` returns any value up to `Integer.MAX_VALUE` for arbitrary attacker-supplied call-data words: [3](#0-2) 

`new byte[len][]` is then allocated immediately with that attacker-controlled `len`, and only afterwards does the loop attempt to read out-of-bounds `words[...]` entries (which would throw `ArrayIndexOutOfBoundsException` and get caught), but the array allocation itself already occurred. A `len` near `Integer.MAX_VALUE` allocates an array of ~2^31 object-reference slots (~16 GB on a 64-bit JVM with compressed oops disabled, ~8 GB with compressed oops), which can throw `OutOfMemoryError` or induce heavy GC pressure/node instability — the same "memory consumption" bug class as the OpenCV `CVE-2017-12602` analog (DoS via unbounded/unchecked allocation size taken directly from untrusted input).

This is directly analogous to `extractBytes32Array`, which has no bound check at all on `len` in any code path: [4](#0-3) 

### Impact Explanation
A crafted `ValidateMultiSign` precompile call (address `0x100000e` equivalent — reachable via a normal `CALL`/`STATICCALL` from any deployed smart contract, triggerable by any unprivileged transaction broadcaster) can force the executing node to allocate a very large `byte[][]` before any size validation occurs. Depending on JVM heap configuration this can throw `OutOfMemoryError`, potentially destabilizing or crashing the node process executing the transaction (including SRs applying the block), which is a Denial-of-Service condition matching the "Impact" criteria (node crash/halt).

### Likelihood Explanation
Reachability depends entirely on the runtime value of `VMConfig.allowTvmSelfdestructRestriction()` for the executing snapshot. This is a committee-activated hard-fork parameter (`allowTvmSelfdestructRestriction`), controlled via `ProposalUtil`/`DynamicPropertiesStore`/`ProposalService`. I was **not able to confirm from the code alone whether this flag is currently active (permanently `true`) on the java-tron mainnet/production network state**, or whether it can still be `false` on some deployed nodes (e.g., freshly bootstrapped nodes syncing from genesis before the corresponding block height, or private/test networks that never activated the proposal). If the flag is already permanently enabled network-wide, this specific unguarded path is not currently reachable in production and the practical severity is reduced to "requires historical/unconfigured deployments." If any production node can still observe `allowTvmSelfdestructRestriction() == false` (e.g., during initial block replay/sync before the activation height, or a private chain not activating this proposal), the bug is fully reachable by an unprivileged contract caller with ordinary energy payment.

### Recommendation
- Move the `sigArraySize > MAX_SIZE` bound check outside the `if (VMConfig.allowTvmSelfdestructRestriction())` guard so it always executes before calling `extractBytesArray`/`extractSigArray`, regardless of the hard-fork flag's state.
- Additionally, harden `extractBytesArray` and `extractBytes32Array` themselves to clamp/reject `len` values that are inconsistent with the actual `data`/`words` array length before allocating (e.g., require `len <= (words.length - offset - 1)`), rather than relying on a downstream `ArrayIndexOutOfBoundsException` to fail safe after the allocation has already happened.

### Proof of Concept
1. Deploy or use a contract that performs a `STATICCALL`/`CALL` to the `ValidateMultiSign` precompiled address, passing raw call data laid out as the standard `(address, permissionId, data, signaturesOffset, ...)` ABI encoding used by `ValidateMultiSign`.
2. Set the word at the computed signature-array-length position (`words[words[3].intValueSafe()/WORD_SIZE]`) to a large value close to `Integer.MAX_VALUE` (e.g. `0x7fffffff`).
3. Execute this call on a node/snapshot where `VMConfig.allowTvmSelfdestructRestriction()` evaluates to `false` for the executing context (e.g. an un-upgraded/private chain, or block-replay before the proposal's effective height).
4. Observe `extractBytesArray(words, offset, rawData)` immediately execute `new byte[len][]` with `len` ≈ 2^31, causing large memory allocation / `OutOfMemoryError` on the executing node before the subsequent `signatures.length > MAX_SIZE` check is ever reached.

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
