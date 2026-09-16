### Title
Unvalidated attacker-controlled length used as array-allocation size in TVM precompiled contract input decoding causes uncontrolled memory allocation (DoS) - ([File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java])

### Summary
The CVE describes an integer-signedness bug where an untrusted, attacker-influenced length value (`i2d_X509()` returning `-1`) is used directly as a `malloc()` size without validation, causing an out-of-bounds/huge allocation and crash. The java-tron analog is in the TVM precompiled-contract input parsers `extractBytes32Array`, `extractBytesArray`, and `extractSigArray` in `PrecompiledContracts.java`, which take a length field straight from attacker-supplied calldata and use it, unchecked against the real remaining buffer size, to allocate a Java array.

### Finding Description
`extractBytes32Array`, `extractBytesArray`, and `extractSigArray` read a length word from the caller-controlled `DataWord[] words` (decoded from the raw calldata passed to a precompiled contract call) via `words[offset].intValueSafe()` and immediately use that value as an array-allocation size: [1](#0-0) [2](#0-1) [3](#0-2) 

`DataWord.intValueSafe()` never returns a negative number — for any 256-bit word that doesn't fit safely in a positive `int`, it returns `Integer.MAX_VALUE`: [4](#0-3) 

This is the direct analog of the libcoap bug: instead of validating a decoded length against the actual size of the underlying data before using it to size an allocation, the code trusts a value taken from fully attacker-controlled bytes (the "length" word of an ABI-style array header inside precompile calldata) and uses it verbatim as `new byte[len][]`. There is no check that `len` is consistent with `words.length` or the actual calldata size before the allocation is attempted — the bounds validation (`offset > words.length - 1`) is done for the *offset*, but the resulting `len` itself is never checked against remaining buffer size before allocating.

### Impact Explanation
Any account can call a smart contract, or send a TVM `CALL`/`STATICCALL`, targeting the `BatchValidateSign` (address `...09`) or `ValidateMultiSign` (address `...0a`) precompiles — enabled behind `VMConfig.allowTvmSolidity059()`, which is an activated chain feature on mainnet — with crafted calldata whose "array length" word is set to a large value (e.g., close to `Integer.MAX_VALUE`, since negative encodings collapse to `Integer.MAX_VALUE` via `intValueSafe()`). This causes an attempt to allocate an array of up to `Integer.MAX_VALUE` reference slots (`new byte[len][]`), leading to `OutOfMemoryError`. Depending on JVM heap state this can crash or hang the node process executing the transaction — a remotely triggerable, unprivileged denial-of-service against any full node/validator that processes the transaction, matching the CVE's "malloc misuse via untrusted size causing DoS" bug class.

### Likelihood Explanation
Reaching this code only requires deploying/calling a contract that invokes the `BatchValidateSign`/`ValidateMultiSign` precompile with attacker-chosen calldata — no special privileges, keys, or SR/witness status are needed. The length field is fully attacker-controlled data inside the transaction's contract-call payload, so exploitation is straightforward for any transaction broadcaster once the relevant `VMConfig` feature is active.

### Recommendation
Validate the decoded `len` in `extractBytes32Array`, `extractBytesArray`, and `extractSigArray` against the actual bounds of `words`/`data` (e.g., ensure `len >= 0` and `offset + len + 1 <= words.length`, and that the computed byte ranges fit within `data.length`) before allocating any array, rejecting the precompile call with an exception instead of attempting the allocation.

### Proof of Concept
1. Deploy/call a contract that issues a `CALL` to precompile address `0x...9` (`BatchValidateSign`) with calldata where the array-length word (used as `len` in `extractSigArray`/`extractBytesArray`) is set to a value that decodes to `Integer.MAX_VALUE` via `intValueSafe()` (e.g., a 256-bit word with the sign bit set or exceeding 4 bytes so it maps to `Integer.MAX_VALUE`).
2. When `PrecompiledContracts` invokes `extractSigArray`/`extractBytesArray` on this data, `new byte[len][]` attempts to allocate a huge array, unguarded by any bound check against the real remaining calldata size.
3. The resulting `OutOfMemoryError` disrupts transaction execution on the node processing the broadcast transaction/block, providing a low-cost, unprivileged DoS vector analogous to CVE-2025-65495.

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
