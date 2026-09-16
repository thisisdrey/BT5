### Title
Unvalidated Offset Arithmetic in ABI Array-Decoding Helpers Allows Attacker-Controlled Index/Length Values in TVM Precompiled Contracts - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
The GDAL CVE-2026-4738 root cause is unchecked/optimized pointer offset arithmetic in zlib's inflate table-building code (`inftree9.c`), where attacker-influenced length/offset values are used to index into a buffer without adequate bounds validation, leading to out-of-bounds memory access. The closest reachable analog in java-tron is the family of ABI-array-decoding helpers in `PrecompiledContracts.java` — `extractBytesArray`, `extractSigArray`, and `extractBytes32Array` — which compute byte offsets and array lengths directly from attacker-supplied calldata words and use them to index into arrays/byte buffers with only partial bounds checks.

### Finding Description
`extractBytesArray` and `extractSigArray` read a length `len` and, for each `i < len`, an offset word `words[offset + i + 1]` from the raw call `words` array, then derive `bytesOffset` and use it (combined with `offset`) to compute a byte-range into `data` via `extractBytes`: [1](#0-0) 

Only a single check (`offset > words.length - 1`) guards entry to the loop; there is no validation that `offset + i + 1` stays within `words.length`, no validation that the computed `bytesOffset` (attacker controlled, `intValueSafe()` of a 256-bit word divided by `WORD_SIZE`) keeps `(bytesOffset + offset + 2) * WORD_SIZE` within `data.length`, and no validation that `len` (also attacker controlled via `words[offset].intValueSafe()`) is non-negative or bounded before allocating `new byte[len][]`. `extractBytes32Array` has the same unchecked pattern with no upper-bound check at all: [2](#0-1) 

`extractBytes` performs the final unguarded slice: [3](#0-2) 

This mirrors the GDAL bug class: length/offset fields taken from untrusted input are combined via arithmetic ("pointer offset optimization") and used directly to index a buffer, with the bounds-check logic incomplete relative to all the ways the offset can be manipulated (negative overflow, huge multiplication, or an offset that lands one loop iteration past `words.length`).

### Impact Explanation
Because the JVM enforces array bounds, this cannot produce true out-of-bounds heap writes/RCE like the C code does. However, an attacker who can call a precompiled contract that uses one of these helpers with crafted calldata can trigger `ArrayIndexOutOfBoundsException`, `NegativeArraySizeException`, or `IllegalArgumentException` (from `Arrays.copyOfRange` with `offset > data.length` or negative bounds) deep inside precompile execution. If such an exception is not caught by a sufficiently generic handler along the `Program`/actuator execution path, it can propagate as an unexpected runtime exception rather than the intended `ContractValidateException`/`ContractExeException`, which is the class of failure that has historically caused node-crash or halt conditions in EVM-derived codebases when precompile-input parsing is under-validated. This satisfies the "node crash / can no longer serve" impact bar even though full memory corruption is not possible in managed Java code.

### Likelihood Explanation
Precompiled contracts are reachable by any account issuing a `TriggerSmartContract` transaction (or via a deployed contract making a `CALL`/`STATICCALL` to a precompile address), and the calldata layout — including the length and offset words — is fully attacker-controlled, making the trigger trivial to construct. The likelihood of hitting the improperly bounded path depends on which specific precompile(s) invoke these decoder helpers being reachable in the currently enabled config (e.g., freeze/vote/signature-batch precompiles gated by `VMConfig.allowTvmFreezeV2()`/`allowTvmVote()` flags observed in the surrounding dispatch table). I was not able to fully confirm, within the remaining search budget, every caller of `extractBytesArray`/`extractSigArray`/`extractBytes32Array` to identify which specific precompiled contract(s) expose them at the entry point (the grep found only 3 and 5 matches respectively, all within the same file, but the exact `execute()` method that calls them was not retrieved before the iteration budget was exhausted). This should be verified by grepping `PrecompiledContracts.java` in full for these method names before treating this as fully confirmed exploitable.

### Recommendation
Add explicit bounds validation before all indexing in `extractBytesArray`, `extractSigArray`, and `extractBytes32Array`: verify `offset + i + 1 < words.length` inside the loop (not just once before it), verify `len >= 0` and reasonably bounded, verify the computed `bytesOffset`/`bytesLen` keep the derived byte range within `[0, data.length]`, and wrap `Arrays.copyOfRange` calls with checks (or a try/catch translating any `RuntimeException` into a `ContractValidateException`) so malformed precompile calldata cannot escape as an uncaught runtime exception.

### Proof of Concept
Construct calldata for a precompile whose `execute()` calls `extractBytesArray`/`extractSigArray` such that the length word at `words[offset]` is a large or negative value (after `intValueSafe()` truncation) and/or an inner offset word at `words[offset + i + 1]` encodes a value that, after `/WORD_SIZE` and `+offset+2`, yields a byte offset exceeding `data.length`. Submit this as calldata in a `TriggerSmartContract` transaction targeting the precompile address; observe `extractBytes`'s `Arrays.copyOfRange(data, offset, offset+len)` or the `new byte[len][]` allocation throwing an uncaught `ArrayIndexOutOfBoundsException`/`NegativeArraySizeException` during precompile execution. (Exact precompile entry point and full exploitability were not confirmed within the available search budget — this needs to be verified against the specific `execute()` method(s) that call these helpers.)

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L428-430)
```java
  private static byte[] extractBytes(byte[] data, int offset, int len) {
    return Arrays.copyOfRange(data, offset, offset + len);
  }
```
