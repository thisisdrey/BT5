Based on the pattern in the kernel CVE (unchecked/attacker-controlled index or length used before validating it against the underlying buffer, leading to out-of-range access and node compromise), the closest reachable analog in java-tron is in the TVM precompiled-contract calldata parsing helpers in `PrecompiledContracts.java`, which allocate and index arrays sized directly from attacker-supplied `DataWord` values with no bound check against the actual size of the input buffer.

### Title
Unvalidated attacker-controlled length used for array allocation/indexing in precompiled-contract calldata parsing - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
`extractBytes32Array`, `extractBytesArray` and `extractSigArray` in `PrecompiledContracts.java` read a length/offset word directly from the raw calldata passed into a TVM precompiled contract call and use it, unchecked, to allocate a Java array and to index into the `words`/`data` buffers. [1](#0-0) 

### Finding Description
`extractBytes32Array(DataWord[] words, int offset)` reads `len = words[offset].intValueSafe()` and immediately does `new byte[len][]`, then loops `i < len` reading `words[offset + i + 1]` with no check that `offset + len` is within the bounds of `words`. [2](#0-1) 

`extractBytesArray` and `extractSigArray` only guard the initial `offset` against `words.length - 1`, but then compute `len`, `bytesOffset`, and `bytesLen` entirely from attacker-controlled `DataWord` values and use them to size/allocate the result array and to call `Arrays.copyOfRange` via `extractBytes`, again without validating that the derived indices stay inside `words` or `data`. [3](#0-2) 

This is structurally the same bug class as the kernel CVE: a value taken from untrusted input (`TPM_ALG_ID` there, an attacker-supplied word here) is used directly as an index/length without a range check, instead of being validated against the true bounds of the underlying data structure before use.

These helpers are private utilities inside `PrecompiledContracts`, used to decode ABI-encoded batch arguments (signature arrays / byte arrays) for the batch-signature-validation family of precompiled contracts (`BatchValidateSign` / `ValidateMultiSign`, addresses `...09` / `...0a`), which are reachable by any address that can trigger a smart contract, since these precompiles are invoked like any other precompiled contract address from TVM `CALL`/`STATICCALL` opcodes when `VMConfig.allowTvmSolidity059()` is enabled.

### Impact Explanation
A crafted `len`/`bytesOffset`/`bytesLen` value (e.g., a very large 256-bit word coerced via `intValueSafe()`) can:
- Trigger `new byte[len][]` with an extremely large `len`, causing excessive memory allocation and `OutOfMemoryError`, which can crash or destabilize the node process.
- Trigger `ArrayIndexOutOfBoundsException`/`NegativeArraySizeException` when the derived offsets exceed the actual `words`/`data` array bounds.

Since this code executes inside TVM contract-call handling (a consensus-critical path shared by all full nodes), an uncaught exception or resource exhaustion here has potential to affect node availability/consensus processing rather than just failing a single unprivileged call — matching the "node crash or halt" impact class accepted by the scan rules.

### Likelihood Explanation
Likelihood is high for reachability: any unprivileged account can deploy or call a smart contract that performs a `CALL`/`STATICCALL` to the `BatchValidateSign`/`ValidateMultiSign` precompile addresses with crafted calldata, requiring no special privileges, SR/witness/committee status, or node compromise — just a normal signed transaction.

I was not able to fully trace, within the tool-call budget, the exact call sites inside `BatchValidateSign.execute()`/`ValidateMultiSign.execute()` that invoke these three helpers (grep only located their definitions and usages within the same file, but I could not pull the specific call-site line numbers before running out of iterations), so I cannot cite the precise `len`-controlling calldata layout for a working end-to-end PoC. This should be verified directly in the repository (or via a Devin session) before treating this as confirmed-exploitable.

### Recommendation
- Add explicit bounds validation in `extractBytes32Array`, `extractBytesArray`, and `extractSigArray`: verify `len >= 0` and that `offset + len` (and all derived `bytesOffset`/`bytesLen` sums) do not exceed `words.length` / `data.length` before allocating or indexing.
- Cap `len` to a sane maximum (e.g., related to max calldata size) to prevent unbounded allocation.
- Return a validation failure (as `isValidAbiEncoding` already does for structural checks) rather than allowing an unchecked runtime exception to propagate from calldata-derived indices.

### Proof of Concept
Conceptual (not fully verified against exact precompile ABI layout due to incomplete call-site tracing):
1. Deploy a trivial contract that performs `staticcall`/`call` to precompile address `0x...09` (`BatchValidateSign`) or `0x...0a` (`ValidateMultiSign`).
2. Craft the calldata's array-length word (the word read as `len` at the relevant `offset`) to an extremely large value (e.g., `0xFFFFFFFF`) while keeping the surrounding words minimal.
3. Submit the transaction; the precompile's array-extraction helper allocates/iterates based on the oversized `len`, producing `OutOfMemoryError` or an out-of-bounds exception during calldata parsing.

Because the exact ABI offsets used by `BatchValidateSign`/`ValidateMultiSign` were not confirmed within this session, a working exploit transaction would need to be constructed and tested against the actual precompile input format before relying on this PoC.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L390-430)
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

  private static byte[] extractBytes(byte[] data, int offset, int len) {
    return Arrays.copyOfRange(data, offset, offset + len);
  }
```
