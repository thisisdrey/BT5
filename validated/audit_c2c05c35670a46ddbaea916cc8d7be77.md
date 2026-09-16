### Title
Unvalidated length/offset fields in `PrecompiledContracts.extractSigArray` / `extractBytesArray` allow out-of-bounds array access from attacker-controlled calldata - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The CVE describes heap-buffer-overflow in GTKWave's FST parser caused by trusting an attacker-supplied `chain_table` length/offset when indexing into a buffer. The closest reachable analog in java-tron is the ABI-array decoding helpers used by TVM precompiled contracts (`ValidateMultiSign`/`BatchValidateSign`), which read length and offset words directly out of caller-supplied calldata and use them to size and index byte arrays without full bounds validation.

### Finding Description
`extractSigArray` and `extractBytesArray` read an item count (`len`) and per-item offsets straight from `DataWord[] words` (derived from the raw `data` parameter of a precompiled-contract call), then use them to allocate an array (`new byte[len][]`) and to compute byte offsets into the original `data` buffer: [1](#0-0) 

`extractBytesArray` has the same pattern with a second untrusted length field (`bytesLen`) taken from `words[offset + bytesOffset + 1]`: [2](#0-1) 

Both ultimately call `extractBytes`, which performs `Arrays.copyOfRange(data, offset, offset + len)` with attacker-controlled `offset`/`len`: [3](#0-2) 

Only a shallow `offset > words.length - 1` guard exists; there is no validation that:
- `len` is non-negative and reasonably bounded (a crafted word can yield a huge or negative `int` via `intValueSafe()`),
- `bytesOffset`/`bytesOffset+offset+2` stays within `words.length`,
- the computed byte range fits inside `data.length`.

A negative `len` triggers `NegativeArraySizeException` in `new byte[len][]`; an out-of-range offset/length triggers `ArrayIndexOutOfBoundsException` in `Arrays.copyOfRange`/`System.arraycopy`. These are unchecked runtime exceptions equivalent in spirit to the heap-overflow root cause in the reported CVE (untrusted length/offset field walked into buffer indexing without bounds checks), just manifesting as a JVM exception instead of native memory corruption.

### Impact Explanation
This code path is reachable by any account issuing a `TriggerSmartContract` (or an internal `CALL`) to the `ValidateMultiSign`/`BatchValidateSign` precompiled contract addresses, i.e., an unprivileged transaction broadcaster or contract deployer, matching the in-scope "TVM opcodes, precompiles" surface. If the resulting exception is not caught somewhere in the precompile-invocation path and propagates as an uncaught error during block/transaction application, it can crash or desynchronize full nodes processing that transaction, since all nodes execute the same deterministic contract call while applying the block — a node crash/halt impact accepted by the validation rules.

I was not able to fully trace, within the remaining tool budget, whether the precompiled-contract invocation site (the caller that dispatches to `ValidateMultiSign`/`BatchValidateSign`) wraps `execute()` in a broad `catch (Throwable)` that safely reverts the transaction instead of propagating the exception. This is the key unresolved question that determines whether the impact is "clean revert" (low severity, not an in-scope finding) or "uncaught exception during block application" (node crash, high severity). This should be verified against the calling code (`Program`/`PrecompiledContracts.execute` dispatch and the surrounding `Runtime`/`Manager` block-application try/catch blocks) before treating this as a confirmed high-severity issue.

### Likelihood Explanation
Triggering the code requires only crafting the `data` payload of a call to the fixed precompile addresses used by `ValidateMultiSign`/`BatchValidateSign` — no special privilege, signature, or state precondition is needed beyond broadcasting a normal smart-contract-triggering transaction, so likelihood of reaching the vulnerable code is high. The likelihood that this produces a serious impact depends entirely on the unresolved exception-handling question above.

### Recommendation
- Add explicit bounds validation in `extractSigArray` and `extractBytesArray` before using `len`/`bytesOffset`/`bytesLen`: reject negative values, reject `len`/`bytesOffset` values that would cause `offset + i + 1` or `bytesOffset + offset + 2` to exceed `words.length`, and reject byte ranges that exceed `data.length`, returning an empty/failure result instead of throwing.
- Ensure the precompiled-contract dispatch path wraps `execute()` calls in a catch-all that converts any `RuntimeException`/`Error` into a normal TVM execution failure (transaction revert) rather than letting it propagate to block-application code.
- Add fuzz/unit tests feeding malformed length and offset words to `ValidateMultiSignContract` and `BatchValidateSignContract` to confirm graceful failure.

### Proof of Concept
Conceptual PoC (needs confirmation against the actual precompile calling convention/address in this codebase):
1. Deploy or call the `ValidateMultiSign` precompiled contract address directly via `TriggerSmartContract` with calldata where the "signature array length" word (consumed as `len` in `extractSigArray`) is encoded as `0xFFFFFFFF...` (a large/negative `int` after `intValueSafe()`), or where an item's offset word points far beyond `data.length`.
2. Submit the transaction; if the resulting `NegativeArraySizeException`/`ArrayIndexOutOfBoundsException` is not caught by the calling code, it propagates uncaught during transaction execution/block application.
3. Observe whether this only causes the transaction to fail as expected (no vulnerability) or whether it destabilizes node execution during block application (needs live-node verification, which is out of scope for static analysis here).

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L428-430)
```java
  private static byte[] extractBytes(byte[] data, int offset, int len) {
    return Arrays.copyOfRange(data, offset, offset + len);
  }
```
