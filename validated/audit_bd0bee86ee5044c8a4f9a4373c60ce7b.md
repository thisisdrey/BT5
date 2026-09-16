### Title
Unbounded array allocation from attacker-controlled length in `ValidateMultiSign`/`BatchValidateSign` precompiles can trigger `OutOfMemoryError` node crash - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The GD `imagewebp` bug is a classic "attacker-controlled size field used to allocate/copy a buffer without an upper bound, causing memory corruption/DoS." The java-tron TVM precompiled contracts `ValidateMultiSign` and `BatchValidateSign` contain the same root-cause pattern: a length value is decoded straight out of caller-supplied `calldata` via `DataWord.intValueSafe()` and used to size a Java array *before* any sanity/`MAX_SIZE` bound is enforced on the legacy (pre-hardfork) code path.

### Finding Description
In `extractBytesArray`/`extractSigArray`, the element count `len` is taken directly from the ABI-encoded calldata and used to allocate a two-dimensional byte array with no upper bound: [1](#0-0) 

These helpers are invoked from `ValidateMultiSign.execute` and `BatchValidateSign.doExecute`. The `MAX_SIZE` bound is only checked *before* extraction when the `allowTvmSelfdestructRestriction` feature flag is active; on the legacy path the array is built first and only checked for `signatures.length > MAX_SIZE` *after* the (potentially huge) allocation has already happened: [2](#0-1) [3](#0-2) 

`intValueSafe()` only clamps to `Integer.MAX_VALUE` when more than 4 bytes are occupied — it does not clamp to any sane "array size" bound — so `len` can be an attacker-chosen value up to `Integer.MAX_VALUE`: [4](#0-3) 

`new byte[len][]` for a multi-gigabyte reference array (`len` near `Integer.MAX_VALUE` needs tens of GB just for the object-reference table) will throw `OutOfMemoryError`, which is an unchecked `Error`, not an `Exception`. `ValidateMultiSign.execute` only wraps the *post-extraction* signature-verification loop in a `try { } catch (Throwable t)` block; the array construction itself happens outside that guard, so the `OutOfMemoryError` propagates out of `execute()` uncaught.

### Impact Explanation
An `OutOfMemoryError` thrown inside precompiled-contract execution, invoked from a single contract call inside `Program.callToPrecompiledAddress`, can destabilize or crash the TVM execution thread/JVM heap for the node processing the transaction, since `OutOfMemoryError` is not a normal, recoverable exception the surrounding VM machinery is designed to swallow gracefully. Because this is reachable from ordinary `TriggerSmartContract` calls to a fixed precompile address, any unprivileged account able to call a contract that reaches `ValidateMultiSign`/`BatchValidateSign` (or call it directly, since these are exposed precompile addresses) can attempt to trigger this condition — i.e., a node-crash / halted-service class of impact.

### Likelihood Explanation
Exploitability is **gated by feature flags**: the pre-check (`sigArraySize > MAX_SIZE`) that would prevent the oversized allocation is only active when `VMConfig.allowTvmSelfdestructRestriction()` is true, and additionally a structural ABI-shape check (`isValidAbiEncoding`) that indirectly bounds counts is only active when `VMConfig.allowTvmOsaka()` is true. If either hardfork flag is active on the running chain, the oversized allocation is blocked before it can occur, per the extensive TIP‑854 regression tests observed in `BatchValidateSignContractTest` and `ValidateMultiSignContractTest`. I was not able to confirm from the code inspected whether these flags are unconditionally enabled (i.e., permanently activated committee proposals) on the current mainnet/production configuration, versus still being feature-gated and toggle-able. This materially affects likelihood, and I could not fully resolve it given the remaining investigation budget.

### Recommendation
- Validate and cap the decoded element count (`len`/`sigArraySize`/`addrArraySize`) against `MAX_SIZE` *unconditionally*, before calling `extractBytesArray`/`extractSigArray`/`extractBytes32Array`, regardless of `allowTvmSelfdestructRestriction` or `allowTvmOsaka` flag state, so legacy code paths are equally protected.
- Add a defensive upper bound check inside `extractBytesArray`/`extractBytes32Array`/`extractSigArray` themselves (e.g., reject `len` above a small sane constant, or bound it by the actual `words.length`) so these helpers are safe regardless of caller discipline.
- Catch `OutOfMemoryError`/`Throwable` around the array-allocation step in precompiled contract `execute()` methods, or better, fail fast with a bounds check before any allocation proportional to attacker-controlled input occurs.

### Proof of Concept
Craft a `batchvalidatesign(bytes32,bytes[],address[])` (or `validatemultisign`) calldata where the ABI "length" word for the `bytes[]` array is set to a large value (e.g., `0x7FFFFFFF`) while running with `allowTvmSelfdestructRestriction` (and `allowTvmOsaka`) disabled:
```
hash:      32 bytes (arbitrary)
offset word for signatures array -> points to a slot whose value = 0x7FFFFFFF (length)
```
Submitting this as calldata to a contract `CALL`/`STATICCALL` against the `BatchValidateSign`/`ValidateMultiSign` precompile address causes `extractBytesArray`/`extractSigArray` to execute `new byte[0x7FFFFFFF][]`, which the JVM cannot satisfy and throws `OutOfMemoryError` before the later `MAX_SIZE` check at [5](#0-4)  is ever reached.

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
