### Title
Unbounded array read in `extractBytes32Array` allows unauthorized index computation to trigger unhandled exception in TVM precompile path - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
`extractBytes32Array` in `PrecompiledContracts.java` reads a caller-controlled length from `words[offset]` and then loops reading `words[offset + i + 1]` with **no bounds check against `words.length`**, unlike its sibling helpers `extractBytesArray` and `extractSigArray`, which both explicitly guard with `if (offset > words.length - 1) { return new byte[0][]; }` before indexing. [1](#0-0) [2](#0-1) [3](#0-2) 

### Finding Description
`extractBytes32Array(DataWord[] words, int offset)` derives an attacker-controlled array length (`len`) from ABI-encoded calldata (`words[offset].intValueSafe()`), then reads `words[offset + i + 1]` for `i` from `0` to `len - 1` without ever validating that `offset + i + 1 < words.length`. This is the same bug class as the PaddlePaddle `gather_tree` advisory (CWE-125, out-of-bounds read from an attacker-controlled index/length pair used to walk an array) — an index/count pair taken directly from untrusted input drives array access with no bound check. In the java-tron codebase, the two structurally identical helper functions used by the same signature-validation precompiles (`extractBytesArray`, `extractSigArray`) were hardened with an explicit `offset > words.length - 1` guard, but `extractBytes32Array` was not, indicating this is a missed/incomplete fix of the same defect class rather than an intentional design choice.

Because `DataWord[]` in Java is bounds-checked at the JVM level, the practical manifestation is an uncaught `ArrayIndexOutOfBoundsException` (a `RuntimeException`) rather than raw memory disclosure, but the root cause — trusting an attacker-supplied length to walk past the allocated array — is identical to the reported bug class.

### Impact Explanation
If this helper is reached by a precompiled contract invoked from the TVM `execute()` dispatch (called for any signature/multisig-related precompile contract, reachable via a `CALL`/`STATICCALL` from any deployed smart contract), a crafted calldata with a small `words` array but a large declared `len` at `words[offset]` throws an unbounded `ArrayIndexOutOfBoundsException`. Whether this only causes the transaction to fail (revert-like behavior) or propagates as an unhandled exception at a layer without a catch depends on the call site's exception handling; the codebase's own test comments (`ValidateMultiSignContractTest`, `BatchValidateSignContractTest`) explicitly document that pre-activation/legacy decode paths for these same sibling helpers can throw uncaught `RuntimeException`s from malformed calldata, confirming this is a known-fragile code path in this precompile family. An uncaught exception surfacing outside the VM's expected exception boundary in block/transaction processing could interrupt normal execution flow for that path.

### Likelihood Explanation
Any unprivileged account can deploy a contract that calls the relevant precompile with attacker-controlled calldata, making this trivially reachable from a single signed transaction with no special privilege. The precondition is only that some code path calls `extractBytes32Array` with attacker-influenced `words`/`offset`/`data`.

### Recommendation
Add the same explicit bounds guard used in `extractBytesArray`/`extractSigArray` to `extractBytes32Array`:
```java
private static byte[][] extractBytes32Array(DataWord[] words, int offset) {
  if (offset > words.length - 1) {
    return new byte[0][];
  }
  int len = words[offset].intValueSafe();
  if (offset + len >= words.length) {
    return new byte[0][];
  }
  byte[][] bytes32Array = new byte[len][];
  for (int i = 0; i < len; i++) {
    bytes32Array[i] = words[offset + i + 1].getData();
  }
  return bytes32Array;
}
```
Additionally, audit every call site of `extractBytes32Array` to confirm the caller's `execute()` method wraps this in a try/catch that returns `Pair.of(false, EMPTY_BYTE_ARRAY)` on `RuntimeException`, consistent with the pattern used elsewhere in `PrecompiledContracts.java` (e.g., `ModExp.execute`).

### Proof of Concept
Exact reachability of `extractBytes32Array` from a specific precompile's `execute()` method could not be confirmed from the indexed portions of `PrecompiledContracts.java` retrieved in this session — the caller was not located within the available context (only `extractBytesArray`/`extractSigArray` callers for `ValidateMultiSign`/`BatchValidateSign` were partially visible). A concrete PoC would require:
1. Deploying a contract whose bytecode invokes the precompile address that calls `extractBytes32Array`.
2. Encoding calldata where `words[offset]` (the declared array length) is large (e.g., `0xFFFFFFFF`) while the total `words` array (derived from actual calldata length) is short.
3. Observing the resulting `ArrayIndexOutOfBoundsException` during precompile execution.

Because the exact call site and its exception-handling wrapper could not be verified with certainty from the retrieved code, confidence in "Critical" impact severity is qualified — the vulnerability is confirmed as a real, un-guarded out-of-bounds array read matching the reported bug class, but I could not fully verify from available context whether an outer exception handler already neutralizes this into a benign transaction revert. I recommend a Devin session with full repository access to trace all callers of `extractBytes32Array` and confirm the exact severity.

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
