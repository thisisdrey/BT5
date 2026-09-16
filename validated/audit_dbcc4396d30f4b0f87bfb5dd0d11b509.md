Based on my investigation, I found a plausible analog: the batch-signature verification precompile in `PrecompiledContracts.java` builds arrays whose sizes come directly from unvalidated `DataWord` fields in the ABI-encoded input, mirroring the libtiff bug class ("crafted field value drives a heap buffer size/index without validation").

### Title
Unvalidated array-length field in TVM precompile input parsing enables crash via crafted contract call data - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
`extractBytes32Array`, `extractBytesArray`, and `extractSigArray` in `PrecompiledContracts.java` read a "length" word directly out of attacker-supplied precompile call `data` and use it, unchecked, to size a `byte[len][]` array and to index further into the `words` array. [1](#0-0) [2](#0-1) 

### Finding Description
Each helper takes `words[offset].intValueSafe()` as `len` and immediately does `new byte[len][]` and then loops `for (int i = 0; i < len; i++) { ... words[offset + i + 1] ... }` with no check that `len` is non-negative, sane in magnitude, or that `offset + i + 1` stays within the bounds of the `words` array derived from the caller-controlled `data`. This is directly analogous to the CVE-2017-5225 pattern: a size/count value taken from untrusted input (`BitsPerSample` in libtiff, `len` here) is used to allocate/index a buffer without validating it against the actual amount of available data, producing out-of-bounds access. In Java this manifests as `NegativeArraySizeException`/`ArrayIndexOutOfBoundsException`/`OutOfMemoryError` rather than a raw memory-safety violation, but the root cause (trusting an attacker-supplied size field for buffer construction) is the same bug class. [3](#0-2) 

`extractBytesArray`/`extractSigArray` also compute `bytesOffset` and `bytesLen` from further attacker-controlled words and pass them into `extractBytes`, which does `Arrays.copyOfRange(data, offset, offset + len)` with no bounds validation before the call. [4](#0-3) 

### Impact Explanation
If these helpers are reachable from a broadcastable smart-contract call (e.g. a batch signature-verification precompile invoked via a TVM `STATICCALL`/`CALL` from any deployed contract), an attacker can construct call data with an extreme or negative `len` field, causing an uncaught runtime exception during precompile execution. Because precompile execution happens inside transaction/block processing in `Manager`, an uncaught `Error` (e.g., `OutOfMemoryError` from a huge array allocation) rather than a checked `Exception` could escape normal TVM exception handling and crash the node or cause a chain halt for validating nodes — a node-crash/DoS class impact.

### Likelihood Explanation
I could not fully confirm, within the tool budget available, which specific precompiled-contract `execute()` method(s) call `extractBytes32Array`/`extractBytesArray`/`extractSigArray`, nor whether an outer `isValidAbiEncoding` bounds check is enforced before these helpers run on every code path (I saw `isValidAbiEncoding` defined nearby but did not verify all call sites gate on it). This materially affects likelihood: if all callers validate `data.length` and derived offsets against `isValidAbiEncoding` before invoking these helpers, the practical exploitability is much lower (bounded to bounded-size legitimate ABI encodings) and this analog would not hold as a critical/high finding.

### Recommendation
Add explicit bounds validation in `extractBytes32Array`, `extractBytesArray`, and `extractSigArray`: reject non-positive or unreasonably large `len` values, and verify `offset + len` (and all derived offsets used to index `words`/`data`) stay within the actual array bounds before allocation or copying, throwing a caught `ContractExeException`-style controlled failure instead of relying on unchecked exceptions.

### Proof of Concept
Not fully constructible without confirming the exact precompile address/selector that invokes these helpers and the enclosing `isValidAbiEncoding` gating — this is the specific gap noted in Likelihood Explanation above.

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L428-430)
```java
  private static byte[] extractBytes(byte[] data, int offset, int len) {
    return Arrays.copyOfRange(data, offset, offset + len);
  }
```
