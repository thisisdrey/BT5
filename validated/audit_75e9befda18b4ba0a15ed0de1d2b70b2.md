Based on my investigation, I found a genuine analog: `extractBytesArray` (used by `ValidateMultiSign` when `allowTvmSelfdestructRestriction()` is disabled) allocates an array sized directly from unvalidated attacker-supplied call data, mirroring the sam2p pattern of trusting an attacker-controlled length before allocation.

### Title
Unbounded array allocation from unvalidated length field in `ValidateMultiSign` precompile - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
The `ValidateMultiSign` precompiled contract (address invoked from any TVM contract) parses a signature-count field directly out of caller-controlled call data and allocates a Java object array of that size in `extractBytesArray`, before any bound check is applied — when the size-limiting `allowTvmSelfdestructRestriction()` feature flag is off.

### Finding Description
`ValidateMultiSign.execute` reads the raw call data and computes an offset into it, then dispatches to one of two helper functions depending on a hard-fork flag: [1](#0-0) 

When `VMConfig.allowTvmSelfdestructRestriction()` is `true`, a `MAX_SIZE` (5) bound is checked before calling `extractSigArray`. However, when the flag is `false`, `extractBytesArray` is called with no prior bound check at all: [2](#0-1) 

`extractBytesArray` reads `len` directly from an attacker-supplied word via `words[offset].intValueSafe()` and immediately allocates `new byte[len][]` before validating it is in-bounds or economically reasonable:
```java
int len = words[offset].intValueSafe();
byte[][] bytesArray = new byte[len][];
```
`intValueSafe()` caps values at `Integer.MAX_VALUE` but does not reject large values [3](#0-2) , so a crafted call payload can set `len` to a very large number (limited only by `Integer.MAX_VALUE`), causing the JVM to attempt allocating a huge `byte[][]` array (referencing element size 4/8 bytes per reference, i.e. up to ~8–16GB for a `MAX_VALUE`-sized array) purely from a length field that has not yet been cross-checked against the actual size of `data`/`rawData`. Only after allocation does the loop iterate up to `len` and read from `words[]`/`data`, which would then throw `ArrayIndexOutOfBoundsException` on truncated data.

This is analogous to `ReadImage`'s `width * height` in sam2p: an untrusted length field is used to size a heap allocation before the actual payload size is validated, so a small malicious input can trigger a hugely disproportionate allocation attempt.

### Impact Explanation
An unbounded `len` drives an oversized array allocation attempt inside the precompile execution path, which every TVM contract call can reach (any address can call a contract that invokes `ValidateMultiSign` via the `validatemultisign` precompile address). This can throw `OutOfMemoryError`/`NegativeArraySizeException` uncaught in a way that could destabilize the node process handling that transaction (the outer `execute()` wraps `Throwable` for the multisign-verification loop, but the array allocation happens *before* entering the try block that catches `Throwable`, at lines 1072-1074, i.e. outside the inner `try` guarding the weight-check loop). An `OutOfMemoryError` can crash or destabilize the executing full node/witness process, which qualifies as a node crash under the "Validate" criteria. This reachable path requires only a single crafted contract call, no special privileges.

### Likelihood Explanation
Reaching this code only requires deploying/calling a contract that invokes the `ValidateMultiSign` precompile with crafted call data while `allowTvmSelfdestructRestriction()` is not yet active for the chain (i.e., pre-hardfork chains, or any network/config where this flag remains disabled). Given `MAX_SIZE`/bound checks were clearly added later specifically for `extractSigArray`, but `extractBytesArray` (the legacy path) was left without an equivalent check, the likelihood of this code path still being live on some deployed networks is non-trivial, though I could not verify from available context which networks currently have `allowTvmSelfdestructRestriction()` enabled by default.

### Recommendation
Apply the same `MAX_SIZE`/length-vs-data-bounds validation performed for `extractSigArray` to `extractBytesArray`: validate `len` against a hard limit and against the actual remaining `words.length`/`data.length` before allocating `new byte[len][]`, regardless of the `allowTvmSelfdestructRestriction()` flag state.

### Proof of Concept
Craft a contract call to the `ValidateMultiSign` precompile address (0x0000...1005 or equivalent) with call data such that:
1. `words[3]` (the signature-array-offset word) points to a location where `words[offset]` (interpreted as `len`) is set to a very large value (e.g., `0x7fffffff`).
2. The chain/test config has `allowTvmSelfdestructRestriction()` disabled (default/legacy config), so `extractBytesArray` is invoked instead of `extractSigArray`.
3. Submit the transaction; the JVM attempts to allocate a `byte[Integer.MAX_VALUE][]` array, likely throwing `OutOfMemoryError`, which is not caught until after the allocation and can affect node stability while processing this attacker-controlled transaction.

**Uncertainty note:** I could not verify from the indexed code whether `allowTvmSelfdestructRestriction()` defaults to enabled on current mainnet/testnet configurations, nor find the exact precompile address mapping or callers of `ValidateMultiSign` to confirm end-to-end reachability from a plain user transaction without additional constraints. Recommend a Devin session with full repo/config access to confirm the current default value of this feature flag and trace the precompile dispatch table before treating this as fully confirmed exploitable on production networks.

### Citations

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
