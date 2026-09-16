### Title
Unbounded Array Length from Attacker-Controlled Calldata Causes OutOfMemoryError/DoS in TVM Precompile Argument Parsing - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
The MQTT bug class in the external report is: a length field taken from untrusted network input is used directly to size/allocate a buffer without validating it against the size of the actual backing data, leading to memory corruption/DoS. The same pattern is reachable in java-tron's TVM precompiled-contract calldata decoder helpers `extractBytes32Array`, `extractBytesArray`, and `extractSigArray` in `PrecompiledContracts.java`, which are used to parse array-shaped arguments (e.g. batch/multi-signature verification) passed to precompiled contracts callable from ordinary smart-contract calls.

### Finding Description
`extractBytes32Array`, `extractBytesArray`, and `extractSigArray` read a "length" word directly from the caller-supplied `DataWord[] words` (i.e., raw TVM calldata converted word-by-word) and use it, unchecked against the real size of `words`/`data`, to allocate a Java array: [1](#0-0) 

`extractBytes32Array` in particular performs **no bounds check at all** before allocating:
```
int len = words[offset].intValueSafe();
byte[][] bytes32Array = new byte[len][];
```
`extractBytesArray`/`extractSigArray` only check that `offset` itself is within `words.length`, but never validate `len` against the remaining calldata size before the allocation `new byte[len][]`.

The `len` value comes from `DataWord.intValueSafe()`: [2](#0-1) 
This clamps only negative/overflowing values to `Integer.MAX_VALUE` — it does **not** validate the value against the actual calldata length. An attacker fully controls this 32-byte calldata word, so `len` can be driven up to `Integer.MAX_VALUE`.

Allocating `new byte[Integer.MAX_VALUE][]` (an array of ~2^31 object references, ~16GB on a 64-bit JVM) or looping to populate such an array causes an immediate `OutOfMemoryError`/excessive heap pressure — directly analogous to the AIS-catcher bug class where an attacker-controlled length field drives an out-of-bounds/oversized memory operation from a single malformed message.

### Impact Explanation
Any account that can submit a transaction invoking a smart contract that in turn calls into the TVM precompile(s) consuming these array-extraction helpers (multi-signature/batch-signature verification precompiles) can supply crafted calldata with an arbitrarily large "array length" word. This triggers an uncontrolled memory allocation attempt inside the node's TVM execution path (reached during ordinary transaction execution / `triggerConstantContract` calls), which can throw `OutOfMemoryError`, destabilize the JVM heap, and crash or hang the executing full node/SR — a Denial of Service against block production or API serving, consistent with the accepted "node crash or halt" impact category.

### Likelihood Explanation
Likelihood is high for the DoS variant: constructing the malicious calldata requires no special privileges — any address that can broadcast a transaction calling the affected precompiled contract can set the relevant length word to a large value. No signature validity or special permission is required to reach the vulnerable parsing code, only a syntactically valid call to the corresponding precompile address.

### Recommendation
Add explicit bounds validation before allocating array of size `len` in all three helper methods: verify `len >= 0` and that `offset + len` (and any subsequent indices `offset + i + 1`, `offset + bytesOffset + 1`) stay within `words.length`, mirroring the bound checks already added in `ContractEventParser.subBytes` (`framework/src/main/java/org/tron/common/logsfilter/ContractEventParser.java`). Reject with a controlled precompile failure (return `Pair.of(false, ...)`) instead of allocating attacker-controlled-sized arrays.

### Proof of Concept
1. Deploy or call a contract that invokes the precompile whose input decoding routes through `extractBytes32Array`/`extractBytesArray`/`extractSigArray` (batch/multi-signature verification precompile).
2. Craft the calldata word at the expected "array length" offset to a large value close to `0x7FFFFFFF` (which survives `intValueSafe()` unmodified since it is positive and fits in 4 bytes).
3. Submit the transaction (or call via `triggerConstantContract`) to a node; the precompile execution path calls the vulnerable helper, which executes `new byte[len][]` with `len` near `Integer.MAX_VALUE`, causing an `OutOfMemoryError` / excessive GC pressure in the node's JVM process.

Note: I could not directly confirm from the indexed snippets which exact precompile contract class(es) invoke `extractBytes32Array` (the caller site was not returned by search, likely due to index size limits) — the analysis is based on the helper methods and their unguarded array-length usage in `PrecompiledContracts.java`, which are only reachable through some precompiled contract's `execute(byte[] data)`. Confirming the exact call sites would require reading the full file, which the current index did not return; a Devin session with full repository access could confirm the exact reachable precompile entry point(s).

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L390-412)
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
```

**File:** common/src/main/java/org/tron/common/runtime/vm/DataWord.java (L219-229)
```java
  /**
   * In case of int overflow returns Integer.MAX_VALUE otherwise works as #intValue()
   */
  public int intValueSafe() {
    int bytesOccupied = bytesOccupied();
    int intValue = intValue();
    if (bytesOccupied > 4 || intValue < 0) {
      return Integer.MAX_VALUE;
    }
    return intValue;
  }
```
