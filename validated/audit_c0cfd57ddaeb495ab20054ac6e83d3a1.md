### Title
Unvalidated array-length fields from calldata drive unbounded allocations in TVM precompiled-contract ABI decoders - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The CVE-2017-16357 bug class is "a size/length field taken from untrusted input is used to size a memory allocation without checking it against the actual available data, leading to memory corruption/invalid free." The same root-cause pattern (attacker-controlled length used directly to size an allocation, with no bound check against the real amount of available data) exists in java-tron's TVM precompiled-contract ABI helper functions `extractBytes32Array`, `extractBytesArray`, and `extractSigArray` in `PrecompiledContracts.java`.

### Finding Description
`extractBytes32Array`, `extractBytesArray`, and `extractSigArray` decode a dynamic array from the raw calldata `data`/`words` passed to a precompiled contract (used by the multi-sig/batch-signature-validation precompiles reachable from TVM `CALL`/`STATICCALL`): [1](#0-0) 

In each function, `len` is read straight from the caller-supplied word (`words[offset].intValueSafe()`) and is used immediately to size a Java array: `byte[][] bytesArray = new byte[len][];` — with no check that `len` fits within `words.length`/`data.length`. `intValueSafe()` is designed to avoid a negative-length `NegativeArraySizeException` from a 256-bit overflow, but it still lets `len` be an arbitrarily large positive int (up to `Integer.MAX_VALUE`) that has no relation to the actual size of the supplied `data`. This mirrors the radare2 flaw where `sh_size` (an attacker/file-controlled size field) was used to size an allocation without validating it against the real section/segment bounds, producing memory corruption via mismatched allocation and use.

Because the loop bodies subsequently read from `words[offset + i + 1]` / `data` using indices derived from this unchecked `len`, a huge `len` also causes uncontrolled iteration and out-of-bounds indexing once `len` exceeds the real backing arrays.

### Impact Explanation
A crafted call to the affected precompiled contract with a malicious length word forces the JVM to attempt an allocation for an oversized `byte[len][]` (each element itself a reference, so even before any inner `byte[]` bodies are populated the outer array alone can demand gigabytes of heap). This is directly triggerable by any account/contract that can invoke the corresponding TVM precompile (multi-sig validation / batch signature validation), i.e. an unprivileged transaction sender or contract caller. The result is an uncontrolled resource-exhaustion / crash condition (`OutOfMemoryError`) inside a code path executed while producing/validating a block, which can affect node availability for that precompile's callers.

### Likelihood Explanation
High reachability: the precompiled contract is invoked via ordinary TVM `CALL` opcode from any deployed/callable contract, requiring only a normal signed transaction with attacker-chosen calldata — no special privileges, node role, or timing needed. The vulnerable helper functions perform no validation before allocation, so triggering the oversized allocation requires only crafting one large word value in the ABI-encoded array-length slot.

### Recommendation
Add explicit bounds checks in `extractBytes32Array`, `extractBytesArray`, and `extractSigArray` before allocating `bytesArray`/`bytes32Array`: verify `len >= 0` and that `offset + len + 1 <= words.length` (and that any derived byte offsets/lengths stay within `data.length`) before calling `new byte[len][...]`, returning an empty array or rejecting execution when the declared length exceeds what the calldata can actually contain — analogous to the `verifyLength(suppliedLength, availableLength)` guard already used in `RLP.decode`.

### Proof of Concept
Not independently executed against the live node in this analysis (no reproduction environment available); the PoC below is a code-path walkthrough based on the cited source:
1. Deploy or use any contract that performs a `CALL`/`STATICCALL` to the precompiled address served by `extractBytesArray`/`extractSigArray` (the multi-sig validation / batch-validate-sign precompile).
2. Craft calldata such that the word at the expected `len` offset decodes (via `intValueSafe()`) to a very large positive value (e.g., close to `Integer.MAX_VALUE`), while the remaining calldata is short/normal size.
3. On execution, `new byte[len][]` (or the subsequent indexed reads using `len`) triggers an `OutOfMemoryError` or out-of-bounds access before any length validation occurs, since no check against `words.length`/`data.length` exists prior to allocation, per `PrecompiledContracts.java:390-426`. [1](#0-0)

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
