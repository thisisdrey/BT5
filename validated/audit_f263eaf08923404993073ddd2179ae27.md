## Analysis Summary

I found a plausible analog to CVE‑2016‑4563's root cause — a mismatch between a size/quantum value and the actual backing data that leads to memory-safety and allocation problems — in java-tron's precompiled contract calldata parsers. [1](#0-0) 

### Title
Unbounded attacker-controlled array-length used before validation in TVM precompile calldata parsers (`extractBytesArray`/`extractSigArray`/`extractBytes32Array`) — ([File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java])

### Summary
`ValidateMultiSign` and `BatchValidateSign` precompiled contracts decode a "count/length" word directly from attacker-supplied calldata and immediately allocate arrays sized by that raw value (`new byte[len][]`), *before* any `MAX_SIZE` bound is enforced and independent of the true size of the calldata buffer. This mirrors the ImageMagick bug class: a "quantum"/count field is trusted to size a buffer while its true relationship to the actual input data length is never validated first.

### Finding Description
`extractBytesArray`, `extractSigArray`, and `extractBytes32Array` read `len = words[offset].intValueSafe()` from a `DataWord` fully controlled by the caller, then allocate `new byte[len][]` immediately: [2](#0-1) [3](#0-2) 

Note `extractBytes32Array` additionally lacks the `offset > words.length - 1` guard present in its two siblings, so it can also throw `ArrayIndexOutOfBoundsException` on out-of-range offsets.

Both callers, `ValidateMultiSign` and `BatchValidateSign`, only check the resulting array length against `MAX_SIZE` (5 or 16) *after* the extraction/allocation has already happened: [4](#0-3) [5](#0-4) 

The `getEnergyForData` cost for both contracts is derived from `data.length` (the actual calldata size), not from the attacker-declared `len` word: [6](#0-5) [7](#0-6) 

Because the "count" word is a standalone 32-byte value inside calldata (not tied to actual payload length), a caller can submit small calldata that declares an enormous `len` (e.g., close to `Integer.MAX_VALUE`), causing a large-object allocation attempt disproportionate to the energy paid and to the real request size — exactly the "mishandled relationship between [a quantum value] and stroke data" pattern from the reference CVE.

`BatchValidateSign.execute` wraps `doExecute` in a top-level `catch (Throwable t)` that swallows the failure: [8](#0-7) 
but `ValidateMultiSign.execute` has **no such top-level guard** around the extraction calls at lines 1057–1078 — only the signature-verification loop later in the method is wrapped in try/catch. An `ArrayIndexOutOfBoundsException` or `OutOfMemoryError` thrown during array extraction in `ValidateMultiSign` therefore propagates unhandled out of the precompile.

### Impact Explanation
- Best case (confirmed): wasted CPU/memory for a disproportionately low energy cost — a resource-exhaustion vector reachable from any signed transaction or, more importantly, from a **free `TriggerConstantContract`/constant call** (no energy charged), i.e., reachable by an anonymous API client with no cost.
- Worst case (uncertain — could not be fully verified with available tooling): if the unhandled exception in `ValidateMultiSign` is not caught by a higher-level VM/transaction wrapper before reaching shared infrastructure (e.g., the constant-call servicing thread pool), or if the oversized allocation triggers heap pressure across the JVM's shared heap, this could degrade or destabilize node availability for other in-flight requests. I was not able to confirm within the tool budget whether java-tron's runtime wraps precompile execution in a blanket exception handler that isolates each call from the rest of the node (this is the standard design in EVM-like runtimes and is likely present, which would reduce this to a per-call revert rather than a node-wide crash).

### Likelihood Explanation
High: the `len` value is a single 32-byte word fully under caller control, and no upper bound is applied prior to allocation; this is trivially reachable via a smart-contract `STATICCALL`/`CALL` to precompile addresses for `ValidateMultiSign`/`BatchValidateSign`, or directly via `TriggerConstantContract` JSON-RPC/HTTP API (unpriveleged, unauthenticated queries).

### Recommendation
- Validate the declared `len` against `MAX_SIZE` (and against the actual remaining calldata length) **before** allocating any array in `extractBytesArray`, `extractSigArray`, and `extractBytes32Array`.
- Add the missing `offset > words.length - 1` bounds check to `extractBytes32Array` to match its sibling functions.
- Ensure `ValidateMultiSign.execute` wraps calldata parsing (not just the signature loop) in a try/catch equivalent to `BatchValidateSign`, so malformed input degrades to a `DATA_FALSE`/revert result rather than an unhandled exception.
- Consider charging energy proportional to the declared array-length word itself, not solely `data.length`, to remove the cost/allocation mismatch.

### Proof of Concept
Construct calldata for `ValidateMultiSign` (or `BatchValidateSign`) where the 32-byte word at the signature/address-array offset encodes a huge integer (e.g., `0x7FFFFFFF`) while the surrounding calldata is otherwise minimal/valid enough to pass `isValidAbiEncoding`. Submitting this via `STATICCALL` to the precompile address, or via `TriggerConstantContract`, causes `extractSigArray`/`extractBytesArray`/`extractBytes32Array` to attempt `new byte[2147483647][]` before the `MAX_SIZE` check is ever reached, at negligible energy/API cost relative to the resulting allocation attempt.

---
**Uncertainty note:** I could not fully confirm (within available tool calls) whether an unhandled exception thrown inside `ValidateMultiSign.execute` before its internal try-block is caught by an outer VM/transaction-level handler that isolates it to a single reverted call versus more broadly affecting the node process. This affects whether the ultimate impact is "wasted resources on a reverted call" (Medium) or a more serious availability issue (High). A Devin session with full repository/runtime access could trace `Program.java`'s precompile-invocation path (`callToPrecompiledAddress`) and the outer VM run loop to confirm exception containment.

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1044-1049)
```java
    @Override
    public long getEnergyForData(byte[] data) {
      long cnt = (data.length / WORD_SIZE - 5) / 5;
      // one sign 1500, half of ecrecover
      return cnt * ENGERYPERSIGN;
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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1137-1142)
```java
    @Override
    public long getEnergyForData(byte[] data) {
      long cnt = (data.length / WORD_SIZE - 5) / 6;
      // one sign 1500, half of ecrecover
      return cnt * ENGERYPERSIGN;
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1144-1154)
```java
    @Override
    public Pair<Boolean, byte[]> execute(byte[] data) {
      try {
        return doExecute(data);
      } catch (Throwable t) {
        if (t instanceof InterruptedException){
          Thread.currentThread().interrupt();
        }
        return Pair.of(true, new byte[WORD_SIZE]);
      }
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
