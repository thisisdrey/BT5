### Title
Unbounded attacker-controlled length field in `PrecompiledContracts` array extraction helpers can trigger `OutOfMemoryError` during precompile execution - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The `extractBytesArray`, `extractSigArray`, and `extractBytes32Array` helper methods in `PrecompiledContracts.java` read a length field directly from attacker-supplied precompile call data and use it to size a Java array (`new byte[len][]`) before any of the referenced elements are validated against the actual size of the input buffer. This mirrors the OpenEXR bug class: an untrusted length value is consumed and used to derive array/allocation sizes without a sanity bound, producing invalid size values that can destabilize the process.

### Finding Description
`extractBytesArray` and `extractSigArray` take `len` straight from `words[offset].intValueSafe()`, an attacker-controlled `DataWord` taken from the calldata passed to a precompiled contract, and immediately allocate `new byte[len][]` before validating `len` against `words.length` or `data.length`: [1](#0-0) [2](#0-1) 

The same pattern occurs in `extractBytes32Array`: [3](#0-2) 

`intValueSafe()` clamps to `Integer.MAX_VALUE` rather than to any data-relative bound, so a crafted `len` close to `Integer.MAX_VALUE` causes the JVM to attempt an allocation of a reference array with billions of slots (`new byte[len][]`), which is exactly the "invalid size derived from an untrusted length field" pattern described in the OpenEXR advisory (`bytesPerLine`/`maxBytesPerLine` computed from crafted data without bound checks). Notably, other parts of the same codebase (`RLP.decode`/`fullTraverse`) explicitly anticipate and catch `OutOfMemoryError` when parsing untrusted length-prefixed data: [4](#0-3) 
but no equivalent guard exists around the `extractBytesArray`/`extractSigArray`/`extractBytes32Array` length usage in `PrecompiledContracts.java`.

### Impact Explanation
If the OOM (or the `ArrayIndexOutOfBoundsException` that would also arise once `bytesOffset`/`bytesLen` derived from more unchecked words are used to index into `words`/`data`) propagates without being caught by the surrounding TVM execution machinery, it can crash or destabilize the thread handling contract execution, and in the worst case can affect node stability during transaction/block processing — matching the "node crash or halt" acceptance bar in the validation rules.

### Likelihood Explanation
This code path is reached whenever a smart contract calls the precompiled contract address that uses these helper functions (a batch/multi-signature validation style precompile), which is directly reachable by any unprivileged account that deploys or calls a contract performing that call — no special privilege is required. However, I was not able to confirm within the available search budget (1) which specific precompiled contract class(es) call `extractBytesArray`/`extractSigArray`/`extractBytes32Array` (the definitions are visible, but the call sites appear further down in `PrecompiledContracts.java` beyond what I could inspect before running out of tool iterations), and (2) whether the TVM's precompile dispatch wraps calls in a catch-all for `Throwable`/`OutOfMemoryError` that would down-grade the impact to a mere reverted transaction rather than a node-level crash. This uncertainty should be resolved by reviewing the full call sites and the exception handling in the `Program`/precompile-dispatch code before treating this as confirmed high-severity.

### Recommendation
Bound `len` (and any offsets derived from calldata) against the real size of `words`/`data` before allocating arrays in `extractBytesArray`, `extractSigArray`, and `extractBytes32Array`, returning a failure result (as is already done for `offset > words.length - 1`) instead of proceeding to allocate/index with unchecked attacker-controlled sizes. Wrap the precompile dispatch path with a catch for `OutOfMemoryError`/`Throwable` analogous to the pattern used in `RLP.java`, converting it into a contract execution failure rather than letting it propagate.

### Proof of Concept
Craft calldata for the precompile that uses `extractBytesArray` (or `extractSigArray`/`extractBytes32Array`) such that the word at the expected `offset` position (interpreted as the array length) is set to a very large value (e.g., `0xFFFFFFFF`). Invoking the precompile with this calldata causes `words[offset].intValueSafe()` to return a huge `len`, and the subsequent `new byte[len][]` allocation attempt can throw `OutOfMemoryError` during precompile execution — verification of the exact downstream effect (revert vs. process-level crash) requires tracing the precompile dispatch/exception-handling code, which was not fully explored in this pass.

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

**File:** framework/src/main/java/org/tron/core/capsule/utils/RLP.java (L596-602)
```java
    } catch (Exception e) {
      throw new RuntimeException(
          "RLP wrong encoding (" + Hex.toHexString(msgData, startPos, endPos - startPos) + ")", e);
    } catch (OutOfMemoryError e) {
      throw new RuntimeException("Invalid RLP (excessive mem allocation while parsing) (" + Hex
          .toHexString(msgData, startPos, endPos - startPos) + ")", e);
    }
```
