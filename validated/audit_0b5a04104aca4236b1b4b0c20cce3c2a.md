### Title
Unbounded array index read in `PrecompiledContracts.extractBytes32Array` can throw uncaught `ArrayIndexOutOfBoundsException` from attacker-controlled calldata - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
`extractBytes32Array` reads an attacker-controlled length word from ABI-encoded precompile calldata and then indexes into the `words` array that many times without ever checking the length against `words.length`, mirroring the CalcMinMax pattern in the CVE (trusting an embedded length field to index a buffer without validating it against the buffer's actual bounds).

### Finding Description
`extractBytes32Array` is a helper used to decode a dynamic `bytes32[]`-style array out of the `DataWord[]` produced from a precompile's calldata: [1](#0-0) 

```
390|  private static byte[][] extractBytes32Array(DataWord[] words, int offset) {
391|    int len = words[offset].intValueSafe();
392|    byte[][] bytes32Array = new byte[len][];
393|    for (int i = 0; i < len; i++) {
394|      bytes32Array[i] = words[offset + i + 1].getData();
395|    }
396|    return bytes32Array;
397|  }
```

`len` is fully attacker-controlled (it's a 256-bit word from the calldata, safely clamped to `int` via `intValueSafe()`, but never checked against `words.length`). The loop then unconditionally accesses `words[offset + i + 1]` for `i` up to `len - 1`. If the caller supplies a length value larger than the number of actual 32-byte words present in `data`, this throws an unguarded `ArrayIndexOutOfBoundsException`.

This is structurally identical to the two sibling helpers in the same file, `extractBytesArray` and `extractSigArray`, which both explicitly guard against this case with `if (offset > words.length - 1) { return new byte[0][]; }` before reading `len` — showing the developers were aware of and mitigated this exact class of bug elsewhere, but missed it in `extractBytes32Array`: [2](#0-1) 

Unlike `extractBytesArray`/`extractSigArray`, `extractBytes32Array` has no equivalent `offset > words.length - 1` guard and no per-item bound check, so both the initial `words[offset]` read and every subsequent `words[offset + i + 1]` read can go out of bounds for a maliciously short `data` array combined with a large encoded length.

### Impact Explanation
An `ArrayIndexOutOfBoundsException` thrown inside a precompiled contract's `execute()` is a Java `RuntimeException`. Depending on whether the call site wraps precompile execution in a broad `try/catch(Exception|RuntimeException)` (as is done for e.g. `ValidateMultiSign`/`BatchValidateSign` per TIP-854 hardening seen in the test suite) or not, this can either be caught and turned into a revert, or propagate up uncaught through the TVM's precompile dispatch path. If any current caller of `extractBytes32Array` lacks such a guard, a crafted transaction/contract call reaching that precompile can crash node processing of that transaction, potentially destabilizing that block's execution or causing inconsistent handling between nodes (liveness/DoS risk), which is the closest in-scope-severity analog to the ImageMagick DoS (crafted-input parsing leading to unhandled out-of-bounds access and crash).

### Likelihood Explanation
Reachability requires identifying which live precompiled contract on the current `getContractForAddress` dispatch table calls `extractBytes32Array` with attacker-supplied `data`/`offset` and confirming whether that call path is guarded by an outer try/catch. I was not able to conclusively confirm the exact current call site and its exception-handling wrapper within this investigation (tool budget exhausted before locating call-site line numbers), so likelihood is uncertain — this needs to be verified in a live session before treating it as confirmed-exploitable.

### Recommendation
Add the same defensive check used by `extractBytesArray`/`extractSigArray` to `extractBytes32Array`: validate `offset` against `words.length` before reading `words[offset]`, and validate `offset + len` against `words.length` before the loop, returning an empty array (or failing the precompile call) instead of throwing. Also audit every precompile method that calls `extractBytes32Array` to ensure malformed calldata cannot propagate an uncaught exception out of `execute()`.

### Proof of Concept
Conceptually: call the precompiled contract's `execute()` with an ABI-encoded input whose fixed-size head contains a huge count/length word (e.g., `0xFFFFFFFF`) at the offset consumed by `extractBytes32Array`, while providing a short `data` array that does not contain that many trailing words. On execution, `len` will exceed `words.length`, and the loop will throw `ArrayIndexOutOfBoundsException` on `words[offset + i + 1]` before any bounds check occurs — this cannot be confirmed as unguarded end-to-end without locating and inspecting the exact calling method's try/catch, which should be done in a full-repository session.

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
