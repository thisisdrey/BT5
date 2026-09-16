Based on my investigation, I found a strong analog in the TVM precompiled contracts, similar in nature to the swtpm bug: an untrusted length field read from input data is used to size an array and index into it without validating the length is in-bounds relative to the input.

### Title
Out-of-bounds array read/DoS via unchecked length field in PrecompiledContracts array extraction helpers - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
`extractBytesArray`, `extractSigArray`, and `extractBytes32Array` in `PrecompiledContracts.java` read an attacker-controlled 32-byte "length" word from the precompile call data and use it directly to size arrays and index further into the `words`/`data` arrays, without validating that the declared count/offset actually fits within the available input. [1](#0-0) 

### Finding Description
`extractBytes32Array(DataWord[] words, int offset)` reads `len = words[offset].intValueSafe()` and then loops `i` from `0` to `len`, accessing `words[offset + i + 1]` with no check that `offset + len` is within `words.length`. [2](#0-1) 

`extractBytesArray` and `extractSigArray` do check `offset > words.length - 1` up front, but this only guards the very first read of the length word — it does not bound the subsequent loop that accesses `words[offset + i + 1]` and `words[offset + bytesOffset + 1]`, both of which are derived from attacker-controlled `len`/`bytesOffset` values decoded from call data. [3](#0-2) 

This is the same bug class as CVE-2022-23645: a header/length field taken from untrusted input is trusted to index into a buffer without validating it against the buffer's actual bounds, producing an out-of-bounds access once the crafted length exceeds the real array size.

### Impact Explanation
An out-of-range `words[offset + i + 1]` access throws an uncaught `ArrayIndexOutOfBoundsException` inside the precompiled-contract execution path. Depending on how the surrounding TVM opcode dispatch/precompile invocation handles unexpected runtime exceptions versus the expected `Program.Exception` hierarchy, this can escape normal VM revert handling and crash node execution of that transaction/block, i.e. a node crash/DoS reachable from a plain, unprivileged smart-contract call. `PrecompiledContracts` execute methods are invoked directly from contract calls to fixed precompile addresses reachable by any address that can send a transaction/message to those addresses, so this is reachable by an ordinary contract deployer/caller, matching the "unprivileged...contract deployer...TVM opcodes, precompiles" in-scope category.

### Likelihood Explanation
I was not able to fully confirm, within the available tool budget, which specific precompiled contract(s) call `extractBytesArray`/`extractSigArray`/`extractBytes32Array` (the grep for call sites returned only the definition matches, and I could not view the calling precompile class(es) before running out of iterations), nor whether an outer try/catch in `Program`/`VM` dispatch already converts any `ArrayIndexOutOfBoundsException` into a normal VM revert (which would reduce this to a caught, non-crashing exception rather than a node-crash). This is a material gap: many precompile `execute()` paths in this codebase are wrapped by exception handling in the VM interpreter loop, which could downgrade the practical impact from "node crash" to "reverted transaction only" (energy-consumption/no-impact, out of scope). Because I could not verify the calling context/exception handling, I cannot confirm with confidence that this reaches an in-scope "node crash or halt" outcome versus a merely reverted transaction.

### Recommendation
Add explicit bounds validation before using the decoded `len`/`bytesOffset` values to index `words`/`data` in `extractBytesArray`, `extractSigArray`, and `extractBytes32Array` — e.g., verify `offset + len < words.length` (and equivalent bounds for `bytesOffset`/`bytesLen` against `data.length`) and return an empty/failure result instead of proceeding when the declared size exceeds the actual buffer, mirroring the `verifyLength` pattern already used in `RLP.java`. [4](#0-3) 

### Proof of Concept
Could not be fully constructed without confirming the calling precompile and its address/dispatch path (needed to build a concrete transaction that reaches `extractBytesArray`/`extractSigArray`/`extractBytes32Array` with a crafted oversized length word). Conceptually: craft call data to the relevant precompile where the length word at the expected offset is set larger than the number of remaining 32-byte words in `data`, causing the loop in `extractBytesArray`/`extractSigArray`/`extractBytes32Array` to index past the end of the `words` array.

**Caveat:** Because I could not verify (a) which precompile(s) invoke these helper methods, and (b) whether the surrounding VM exception handling already catches `ArrayIndexOutOfBoundsException` and converts it to a safe revert, I cannot confirm this rises to the required "node crash or halt" bar rather than being a caught/no-impact exception. If time permits further investigation (finding the callers of `extractBytesArray`, `extractSigArray`, `extractBytes32Array`, and the exception handling in `Program`/`VM`/`OperationActions`), this should be re-verified before treating it as a confirmed Medium/High finding.

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

**File:** framework/src/main/java/org/tron/core/capsule/utils/RLP.java (L612-617)
```java
  private static void verifyLength(int suppliedLength, int availableLength) {
    if (suppliedLength > availableLength) {
      throw new RuntimeException(String.format("Length parsed from RLP (%s bytes) is greater "
          + "than possible size of data (%s bytes)", suppliedLength, availableLength));
    }
  }
```
