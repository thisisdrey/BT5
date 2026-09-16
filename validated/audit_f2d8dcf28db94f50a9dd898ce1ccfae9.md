Based on my investigation, I found a concrete analog in `PrecompiledContracts.java`.

### Title
Uncontrolled array allocation in TVM precompile ABI decoding leads to node OOM/crash - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
The `extractBytes32Array`, `extractBytesArray`, and `extractSigArray` helper methods in `PrecompiledContracts.java` read an array-length field directly from attacker-supplied precompile call data and use it, unbounded, to allocate a Java array before any relationship between the declared length and the actual size of the input `data` is validated.

### Finding Description
`extractBytes32Array` reads `len = words[offset].intValueSafe()` and immediately allocates `new byte[len][]` with no check that `len` is consistent with the size of `words` (the actual calldata array) [1](#0-0) . `extractBytesArray` and `extractSigArray` follow the identical pattern: they bound-check only `offset` against `words.length`, then take `len = words[offset].intValueSafe()` and allocate `new byte[len][]` before verifying `len` against `words.length` [2](#0-1) . `DataWord.intValueSafe()` clamps the 256-bit word into an `int`, so a caller can freely choose any value up to `Integer.MAX_VALUE` for this length field. Allocating `new byte[Integer.MAX_VALUE][]` (an object-reference array) requires several gigabytes of heap merely for the array of null references, before any element is populated — this mirrors the `protobuf` `Vec::reserve()` bug class described in the report: a length value taken from untrusted input is used directly to pre-size a collection with no upper bound tied to the real payload size, letting the caller force a large allocation cheaply.

This is reachable by any account: precompiled contracts are invoked via ordinary `CALL`/`STATICCALL` opcodes from a smart contract, meaning any transaction sender who deploys or calls a contract that hits one of these ABI-decoding precompile paths can supply the malicious length word.

### Impact Explanation
An attacker-controlled allocation of this magnitude can trigger an `OutOfMemoryError` in the node's JVM, crashing or destabilizing the full node/validator process that executes the transaction — a denial-of-service against block production/serving, which is explicitly an accepted impact category (node crash or halt).

### Likelihood Explanation
Likelihood depends on which precompile actually calls these helpers with attacker-influenced offsets and on the associated energy metering in `getEnergyForData`. I was not able to fully confirm, within the remaining investigation budget, which concrete precompile (e.g., `BatchValidateSign`/`ValidateMultiSign`) invokes `extractBytesArray`/`extractSigArray` with an offset that is directly derived from the caller's raw `data`, nor whether the energy cost model for that precompile scales with the declared array length before this allocation happens. This is a meaningful gap: if energy cost is charged proportional to `data.length` (bounding the caller's cost) and the `len` value is always tightly coupled to real data bounds by an earlier validation elsewhere in the call chain, the practical exploitability would be reduced. This distinction could not be verified from the indexed snippets alone.

### Recommendation
Validate `len` against the actual bounds of `words`/`data` (e.g., `len >= 0 && offset + 1 + len <= words.length`) before calling `new byte[len][]`, mirroring the bounds checks already present in `RLP.java`'s `verifyLength` pattern [3](#0-2) . Apply the same fix uniformly to `extractBytes32Array`, `extractBytesArray`, and `extractSigArray`.

### Proof of Concept
Not fully constructible without confirming the exact precompile entry point and its energy-cost gating, per the likelihood caveat above; a background Devin session with full repo/build access would be needed to trace the call graph into `extractBytesArray`/`extractSigArray`/`extractBytes32Array` and craft a concrete calldata payload.

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

**File:** framework/src/main/java/org/tron/core/capsule/utils/RLP.java (L612-617)
```java
  private static void verifyLength(int suppliedLength, int availableLength) {
    if (suppliedLength > availableLength) {
      throw new RuntimeException(String.format("Length parsed from RLP (%s bytes) is greater "
          + "than possible size of data (%s bytes)", suppliedLength, availableLength));
    }
  }
```
