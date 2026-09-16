## Analog Found: Unbounded array allocation from attacker-controlled length field in TVM precompiles `ValidateMultiSign`/`BatchValidateSign`

### Title
Unvalidated attacker-controlled array-length field causes unbounded heap allocation in TVM precompiled contracts - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The SvelteKit bug class is: a small request expands, via an unvalidated length/count field, into an enormous in-memory structure before any size check is performed, causing expensive processing / OOM. The same pattern exists in java-tron's `ValidateMultiSign` and `BatchValidateSign` precompiled contracts, reachable by any account issuing a `TriggerSmartContract` transaction that `CALL`s these fixed precompile addresses (`0x9`, `0xa`).

### Finding Description
`extractBytesArray`, `extractSigArray`, and `extractBytes32Array` read an array length directly from calldata and immediately allocate a Java array of that size, *before* any upper-bound validation: [1](#0-0) 

The size guard (`MAX_SIZE`) is only applied when `VMConfig.allowTvmSelfdestructRestriction()` is enabled, and it is applied to a *separately re-derived* index expression, not to the extraction call itself:

- `ValidateMultiSign.execute` — the `MAX_SIZE` pre-check is gated behind `allowTvmSelfdestructRestriction()`; when that flag is off, `extractBytesArray` is invoked directly with an attacker-supplied `len`, and this call sits *outside* the method's own `try { ... } catch (Throwable t)` block, so an `OutOfMemoryError` from `new byte[len][]` is not locally absorbed: [2](#0-1) 

- `BatchValidateSign.doExecute` — `addresses = extractBytes32Array(words, words[2].intValueSafe() / WORD_SIZE)` is called **unconditionally**, independent of the `allowTvmSelfdestructRestriction()` feature flag that gates the `addrArraySize` bound check: [3](#0-2) 

`len` comes from `DataWord.intValueSafe()`, an attacker-fully-controlled 256-bit calldata word truncated/clamped to an `int`, so a request only a few hundred bytes long (just enough to encode the ABI head plus one offset/length word) can request allocation of an array with up to `Integer.MAX_VALUE` (or a very large) number of elements — mirroring the "small input expands into large array" mechanism from the SvelteKit `files.length` bug.

### Impact Explanation
Triggering `new byte[Integer.MAX_VALUE][]` (or similarly huge sizes) throws `OutOfMemoryError`. In `BatchValidateSign.execute` this is swallowed by an outer `catch (Throwable t)`, but in `ValidateMultiSign.execute` the extraction call is outside any local catch, so the error propagates into VM opcode dispatch / `Program.callToPrecompiledAddress`. Even where it is locally caught, repeated concurrent invocations (each requesting large transient allocations before the JVM can throw) create heap pressure/GC churn shared across the whole node process, since this precompile can be invoked from any contract call in any transaction, by any unprivileged sender, at very low fixed energy cost — the `getEnergyForData` formula only charges for the *nominal* per-signature cost, not for the cost of the oversized length field used purely to trigger allocation. This can degrade or crash the node (denial of service for the query/transaction-processing pipeline), consistent with the CVE-2026-82259 impact class (DoS via unfiltered length-driven expansion).

### Likelihood Explanation
Reachable via a single, ordinary, unprivileged `TriggerSmartContract` transaction that issues a raw `CALL` to address `0x9` (`ValidateMultiSign`) or `0xa` (`BatchValidateSign`) with crafted calldata — no special permissions, existing account state, or deployed contract needed. The vulnerable path is currently guarded by the `allowTvmSelfdestructRestriction` hard-fork flag for most extraction sites, so exploitability on a live network depends on whether/when that flag activates; however, the `BatchValidateSign` unconditional `extractBytes32Array` call for the address array is not gated by that flag at all, making it live regardless of fork status.

### Recommendation
- Validate `len` against `MAX_SIZE` (or a sane upper bound, e.g. against `words.length`) inside `extractBytesArray`, `extractSigArray`, and `extractBytes32Array` themselves, before allocating, rather than relying on external call-site checks that are inconsistently applied.
- Remove the fork-flag gating for the bounds check on the `BatchValidateSign` address array so it is always validated before `extractBytes32Array` executes.
- Wrap the extraction calls in `ValidateMultiSign.execute` with the same defensive `catch (Throwable)` used elsewhere, or better, bound the allocation size unconditionally so the error condition cannot occur at all.

### Proof of Concept
Construct calldata for `validatemultisign(address,uint256,bytes32,bytes[])` (or `batchvalidatesign`) such that the ABI head's offset word for the `bytes[]`/`address[]` parameter points to a word whose value is a very large integer (e.g. `0x7FFFFFFF`), while keeping the rest of the payload minimal (a few hundred bytes total). Submit this as calldata to a `CALL` targeting precompile address `0x...9` or `0x...a` inside a `TriggerSmartContract` transaction on a network where `allowTvmSelfdestructRestriction` (TIP-854) is not yet active — `extractBytesArray`/`extractBytes32Array` will attempt `new byte[0x7FFFFFFF][]`, throwing `OutOfMemoryError` from a payload far smaller than the resulting allocation attempt.

### Citations

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
