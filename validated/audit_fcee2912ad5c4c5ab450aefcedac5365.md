## Analog Vulnerability Found

### Title
Out-of-bounds array access in `BatchValidateSign` precompile ABI-array parsing due to missing offset/length range checks - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
The CVE-2022-39317 bug class is "missing range check for an input offset/index before using it to read a buffer, allowing a remote party to trigger an out-of-bounds read." The java-tron analog is in the helper functions `extractBytesArray` and `extractSigArray` used by the `BatchValidateSign` precompiled contract, which parse attacker-supplied ABI-encoded `bytes[]`/signature arrays from smart-contract call data without validating that the decoded offsets/lengths stay within the bounds of the `words` array or the `data` buffer.

### Finding Description
`extractBytesArray` and `extractSigArray` decode a dynamic array header from the `words` array (the calldata split into 32-byte `DataWord`s) and then index further into that array using attacker-controlled values pulled directly from the same call data: [1](#0-0) 

Specifically:
- `len = words[offset].intValueSafe()` is fully attacker-controlled and is used to bound a loop from `0` to `len`, indexing `words[offset + i + 1]` with **no check** that `offset + i + 1 < words.length`.
- `bytesOffset = words[offset + i + 1].intValueSafe() / WORD_SIZE` is also attacker-controlled and is used to index `words[offset + bytesOffset + 1]` (in `extractBytesArray`) and to compute a byte offset into the raw `data` buffer for `extractBytes(data, (bytesOffset + offset + 2) * WORD_SIZE, bytesLen)`.
- `extractBytes` performs `Arrays.copyOfRange(data, offset, offset + len)` with no validation that `offset`/`offset+len` are within `data.length`.

The only guard present is `if (offset > words.length - 1) return new byte[0][];` at function entry — this checks only the very first index and does nothing to bound the subsequent attacker-controlled offsets (`i`, `bytesOffset`, `bytesLen`) derived from the decoded `DataWord`s. This mirrors the ZGFX decoder flaw: a length/offset value taken from untrusted input is used to index a buffer without validating it against the buffer's actual size.

### Impact Explanation
A crafted call to the `BatchValidateSign` precompiled contract (reachable by any contract deployer/caller through a `CALL`/`STATICCALL` TVM opcode targeting the precompile address) can supply a `len`, `bytesOffset`, or `bytesLen` value that forces `words[...]` or `Arrays.copyOfRange(data, ...)` to be accessed out of bounds, throwing an uncaught `ArrayIndexOutOfBoundsException` or `NegativeArraySizeException`/`IllegalArgumentException` during precompile execution. Because this occurs deep inside TVM opcode dispatch for precompiled contracts (in-scope per the report's TVM opcodes/precompiles/energy-metering criterion), an unhandled runtime exception at this layer can escape the normal energy/exception handling paths used for TVM reverts, resulting in denial of service for the executing node during transaction/block processing.

### Likelihood Explanation
Reaching this code only requires broadcasting a standard signed transaction that performs a `CALL` to the `BatchValidateSign` precompiled contract address with attacker-chosen call data — no special privileges, witness/SR status, or network position needed. This makes it trivially reachable by any unprivileged contract caller.

### Recommendation
Add explicit bounds validation before every indexed access in `extractBytesArray` and `extractSigArray`:
- Validate `offset + i + 1 < words.length` inside the loop before reading `words[offset + i + 1]`.
- Validate `offset + bytesOffset + 1 < words.length` before reading `words[offset + bytesOffset + 1]`.
- Validate that `(bytesOffset + offset + 2) * WORD_SIZE + bytesLen <= data.length` and `bytesLen >= 0` before calling `extractBytes`.
- Consider capping `len` to a sane maximum and returning a "false"/failure result to the calling contract rather than throwing, consistent with how `Sig.execute` and other precompiles report validation failures.

### Proof of Concept
Construct calldata for `BatchValidateSign(bytes32,bytes,bytes[])` (or the specific signature used by `extractBytesArray`/`extractSigArray` callers) where:
1. The header word at the array's `offset` slot encodes a large `len` (e.g., `0xFFFFFFFF`), causing the loop to read `words[offset + i + 1]` far past the actual `words.length` computed from the (short) calldata, triggering `ArrayIndexOutOfBoundsException`.
2. Alternatively, keep `len` small but set the per-item offset word to a large value so that `bytesOffset` computed from it, combined with `(bytesOffset + offset + 2) * WORD_SIZE`, exceeds `data.length`, causing `Arrays.copyOfRange` in `extractBytes` to throw.

Exact reproduction requires locating the concrete precompile entry point (address/signature) that invokes `extractBytesArray`/`extractSigArray`, which the test file [2](#0-1)  exercises for the legitimate path; the index search did not return the exact caller site's line numbers, so full call-graph confirmation (which specific `execute()` method invokes these two helpers, and whether that call path is wrapped in a broad `try/catch` that safely reverts rather than crashing the node) could not be fully verified within the available searches.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L399-426)
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

**File:** framework/src/test/java/org/tron/common/runtime/vm/BatchValidateSignContractTest.java (L1-1)
```java
package org.tron.common.runtime.vm;
```
