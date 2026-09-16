## Analog Found

### Title
Unvalidated Large Array-Length Field Causes Crash-Inducing Allocation in TVM Precompiled Contract Argument Decoding - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The Fiber advisory describes a decoder that reads an attacker-controlled numeric index from user input and uses it directly to size a slice (`reflect.MakeSlice(t, idx+1, idx+1)`) without bounding it, causing a crash. `PrecompiledContracts.java` contains the same pattern in its ABI-argument extraction helpers: `extractBytes32Array`, `extractBytesArray`, and `extractSigArray` read a length field directly from caller-supplied `DataWord[]` call data (`words[offset].intValueSafe()`) and immediately use it to allocate a Java array (`new byte[len][]`) with no check that `len` is consistent with the actual size of `words`/`data`.

### Finding Description
In `extractBytes32Array`: [1](#0-0) 

and `extractBytesArray`: [2](#0-1) 

and `extractSigArray`: [3](#0-2) 

each method takes `len` straight from `words[offset].intValueSafe()`, an attacker-controlled 256-bit word taken from the smart-contract call data passed to a TVM precompiled contract, and immediately allocates `new byte[len][]` (or iterates `len` times indexing further into `words`) without validating that `len` is bounded by the remaining size of `words`/`data` or by any sane maximum. This mirrors the Fiber bug class exactly: a numeric field from untrusted input is used unchecked as an allocation size / loop bound, rather than being validated against the actual buffer length first.

Because these helpers are invoked from `execute()` implementations of specific `PrecompiledContract` subclasses reachable from any TVM `CALL`/`STATICCALL` to the corresponding precompile address, any unprivileged account that can deploy or invoke a smart contract can trigger this code path by crafting call data with a huge `len` word.

### Impact Explanation
An oversized `len` derived from a 256-bit `DataWord` (via `intValueSafe()`, which only guards against `long`/`int` conversion overflow, not against unreasonable magnitudes) can drive `new byte[len][]` to request a very large array. This either throws `OutOfMemoryError`/`NegativeArraySizeException` inside the TVM precompile execution path, or forces significant heap allocation. Because this executes inside transaction/block processing (`Manager` block application via `Runtime`/TVM), an uncaught `OutOfMemoryError` can crash the node process executing the block, producing a denial-of-service against any full node that processes the malicious transaction — matching the "node crash" impact bucket allowed by the rules.

### Likelihood Explanation
The path is reachable by any address capable of submitting a `TriggerSmartContract` transaction that invokes a contract which in turn issues a `CALL`/`STATICCALL` to the precompiled contract address backed by these extraction helpers — no special privilege, validator/witness role, or peer position is required, consistent with "unprivileged transaction broadcaster" / "contract deployer" reachability required by the rules.

### Recommendation
Before allocating `new byte[len][]` (or looping `len` times over `words`), validate that `len >= 0` and that `offset + len + 1 <= words.length` (and that any derived byte offsets/lengths fit within `data.length`), rejecting the precompiled-contract call (returning failure) rather than allocating an attacker-chosen-size array. Apply the same bound check uniformly across `extractBytes32Array`, `extractBytesArray`, and `extractSigArray`.

### Proof of Concept
1. Deploy a contract that performs a `STATICCALL`/`CALL` to the precompiled-contract address backed by `extractBytesArray`/`extractSigArray` (e.g. the batch/multi-signature validation precompile that uses `extractSigArray`).
2. Craft the call data's array-length word (the `DataWord` at the `offset` consumed by these helpers) to an extremely large value (e.g. close to `Integer.MAX_VALUE`), while keeping the remainder of `data` short.
3. Submit the transaction; when the node executes the precompile, `words[offset].intValueSafe()` returns the huge value, and `new byte[len][]` is allocated without a bounds check, triggering `OutOfMemoryError`/`NegativeArraySizeException` during block execution, potentially crashing the executing node.

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
