### Title
Unvalidated attacker-controlled "count" fields drive unbounded array allocation / out-of-bounds indexing in TVM precompiled-contract array extraction helpers - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
The reported Imager CVE is a class of bug where a size/count field taken from untrusted structured input is trusted and used directly to size or bound a subsequent memory operation without first validating it against the amount of data actually available, causing the operation to run past the intended structure. The closest reachable analog in java-tron is in the precompiled-contract input-decoding helpers `extractBytes32Array`, `extractBytesArray`, and `extractSigArray` in `PrecompiledContracts.java`, which read a `len`/count word directly out of attacker-supplied call data and use it, unvalidated, to allocate arrays and index into the decoded `words`/`data` buffers.

### Finding Description
`extractBytes32Array` reads a length directly from the caller-supplied words array and uses it both to allocate an array and to index further into the same words array with no bound check against `words.length`: [1](#0-0) 

`extractBytesArray` performs a similar unvalidated read of `len` from `words[offset]`, then uses per-element offsets and lengths taken from the same untrusted array to call `extractBytes`, which does a raw `Arrays.copyOfRange(data, offset, offset + len)`: [2](#0-1) 

`extractSigArray` has the identical pattern for signature arrays: [3](#0-2) 

In all three helpers, the `len`/`bytesLen`/`bytesOffset` values originate from `DataWord.intValueSafe()` on attacker-controlled call data, and are used to size a `byte[][]` allocation or to compute an `Arrays.copyOfRange` window before any check that the value is consistent with the actual size of `words` or `data`. This is structurally the same root cause as `copy_string_tags()` in the Imager report: a count/size field from the input structure is used to drive a memory operation without first validating it against the real bounds of the buffer it describes. Because Java arrays are bounds-checked, the concrete failure modes differ from a raw heap over-read: an oversized `len` causes either (a) an `OutOfMemoryError` from the `new byte[len][]` allocation (an `Error`, not an `Exception`, which will not be caught by conventional `catch (Exception e)` handling around precompile execution), or (b) an `ArrayIndexOutOfBoundsException`/`NegativeArraySizeException`/`IndexOutOfBoundsException` when the subsequent loop indexes `words[offset + i + 1]` or `Arrays.copyOfRange` is given an out-of-range window.

By contrast, elsewhere in the same file the shielded-transaction precompile explicitly validates count fields (`spendCount`, `receiveCount`) against a strict bound before use: [4](#0-3) 
which shows the codebase's own established mitigation pattern for exactly this class of bug is absent from `extractBytes32Array`/`extractBytesArray`/`extractSigArray`.

### Impact Explanation
If the count field is used to allocate a very large `byte[][]` (e.g., near `Integer.MAX_VALUE`), the JVM must attempt a multi-gigabyte allocation, which can throw `OutOfMemoryError`. Because `OutOfMemoryError` is not a subclass of `Exception`, generic `catch (Exception e)` wrapping around contract/precompile execution will not catch it, and OOM conditions frequently destabilize the whole JVM process rather than a single thread, which can crash or halt the node — a node-crash/denial-of-service impact reachable from a single crafted contract call. The narrower `ArrayIndexOutOfBoundsException` cases are a lower-severity DoS if caught per-transaction, but are still root-caused by the same missing bounds validation.

### Likelihood Explanation
The helper functions are reachable from precompiled-contract call paths inside `PrecompiledContracts.java`, which are invoked whenever any account calls the corresponding precompiled contract address via a normal `TriggerSmartContract`/CALL — i.e., reachable by an unprivileged transaction sender without special permissions. I was not able to fully trace, within the available tool budget, the exact precompiled-contract address/opcode that wires up each of `extractBytes32Array`, `extractBytesArray`, and `extractSigArray` to confirm whether any upstream energy/size limit already caps the practical magnitude of the crafted `len` value before it reaches these helpers, or whether the containing `execute()` method is wrapped in a broad exception handler that would convert an `ArrayIndexOutOfBoundsException` into a safe revert. This should be verified directly against the call sites before treating this as fully confirmed exploitable.

### Recommendation
Validate the extracted `len` (and per-element `bytesOffset`/`bytesLen`) against the actual bounds of `words`/`data` before using them to size allocations or index arrays in `extractBytes32Array`, `extractBytesArray`, and `extractSigArray`, following the same explicit bound-check pattern already used for `spendCount`/`receiveCount` in the shielded-transaction precompile. Reject the call (return a failure result) rather than allocate or index based on an unchecked untrusted count field.

### Proof of Concept
Not independently reproduced against a running node within this session; the root-cause is demonstrated statically: any call to a precompiled contract that decodes its input via `extractBytesArray`/`extractBytes32Array`/`extractSigArray` with an ABI count word crafted to a very large positive integer (e.g., `0x7fffffff`) would drive `new byte[len][]` to attempt an oversized allocation, or drive the subsequent loop to index past the actual `words` array length, before any bounds validation occurs.

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L399-430)
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

  private static byte[] extractBytes(byte[] data, int offset, int len) {
    return Arrays.copyOfRange(data, offset, offset + len);
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1492-1499)
```java
        int spendCount = parseInt(data, spendOffset);
        int spendAuthSigCount = parseInt(data, spendAuthSigOffset);
        int receiveCount = parseInt(data, receiveOffset);

        if (spendCount != spendAuthSigCount || spendCount < 1
            || spendCount > 2 || receiveCount < 1 || receiveCount > 2) {
          return Pair.of(true, DataWord.ZERO().getData());
        }
```
