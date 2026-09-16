This is the `BatchValidateSign` precompiled contract, reachable by any TVM contract call. This is a genuine analog to CVE-2016-8680's out-of-bounds read pattern: an attacker-controlled length field drives array indexing without a preceding bounds check against the actual backing array size.

### Title
Out-of-bounds read / `ArrayIndexOutOfBoundsException` DoS in `BatchValidateSign` precompile via unchecked address-array length — ([File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java])

### Summary
`PrecompiledContracts.extractBytes32Array` reads an attacker-controlled length word from the ABI-encoded call data and then indexes directly into the `words` array that many times, with no check that `offset + len` stays within `words.length`. This mirrors the libdwarf `_dwarf_get_abbrev_for_code` bug class: a size/index value taken from untrusted input is used to index a backing array without validating it against the array's actual bounds first.

### Finding Description
In `PrecompiledContracts.java`:
```java
private static byte[][] extractBytes32Array(DataWord[] words, int offset) {
  int len = words[offset].intValueSafe();
  byte[][] bytes32Array = new byte[len][];
  for (int i = 0; i < len; i++) {
    bytes32Array[i] = words[offset + i + 1].getData();
  }
  return bytes32Array;
}
``` [1](#0-0) 

Unlike its sibling helpers `extractBytesArray` and `extractSigArray`, which both guard with `if (offset > words.length - 1) { return new byte[0][]; }` before reading `words[offset]`, `extractBytes32Array` performs no such bounds check at all, either on `offset` or on the derived `len`: [2](#0-1) 

`extractBytes32Array` is invoked from the `BatchValidateSign` precompile's `doExecute`, where `offset` and the address-array length are both fully attacker-controlled ABI offsets/words parsed from raw calldata:
```java
byte[][] addresses = extractBytes32Array(
    words, words[2].intValueSafe() / WORD_SIZE);
``` [3](#0-2) 

The only length cap (`MAX_SIZE`) is applied to `sigArraySize`/`addrArraySize` solely under the `VMConfig.allowTvmSelfdestructRestriction()` feature flag: [4](#0-3) 

If that flag is disabled (older/alternate hard-fork configuration), `extractBytes32Array` is reached with a completely uncapped `len`, and a crafted `words[2]` offset combined with a large `len` value will cause `words[offset + i + 1]` to throw `ArrayIndexOutOfBoundsException` when `offset + i + 1 >= words.length`, or, for a negative/huge `len` derived from `intValueSafe()`, a `NegativeArraySizeException` when allocating `new byte[len][]`.

### Impact Explanation
`BatchValidateSign` is invoked as a normal Solidity precompile call (address `0x66` region) from any smart contract, meaning any unprivileged transaction sender who can deploy or call a contract that invokes this precompile can trigger the crash. An uncaught `ArrayIndexOutOfBoundsException`/`NegativeArraySizeException` propagating out of TVM execution is a node-crash risk — if it escapes normal `RuntimeException` handling in `VM.play`, it can halt block processing for the node executing the transaction. Even if caught, it represents an availability degradation and potential consensus-relevant divergence in how different node versions handle the failure. This is scoped strictly to the reachable precompile path called out in the task (TVM precompiles/energy metering).

### Likelihood Explanation
`extractBytesArray`/`extractSigArray` were clearly patched to bounds-check `offset` against `words.length` for exactly this reason, but the equivalent guard was never added to `extractBytes32Array`, which strongly suggests this is an overlooked/incomplete fix rather than an intentionally-safe path. The trigger requires only crafting the `CALL` data to the precompile address with an oversized `words[2]`-derived offset/length — no privileged role or special network conditions.

### Recommendation
Add the same `if (offset > words.length - 1) return new byte[0][];` guard to `extractBytes32Array`, and additionally clamp/validate `len` (e.g., reject if `offset + len + 1 > words.length` or if `len` is negative/exceeds `MAX_SIZE`) unconditionally, not just when `allowTvmSelfdestructRestriction()` is enabled.

### Proof of Concept
Craft calldata to the `BatchValidateSign` precompile address where:
- `words[0]` = arbitrary hash (32 bytes)
- `words[1]` = a small, valid signature-array offset
- `words[2]` = an offset value such that `words[2]/WORD_SIZE` points near the end of the `words` array
- The word at that offset (used as `len`) is set to a large value (e.g., `0x7fffffff` or a negative-decoding value)

When `allowTvmSelfdestructRestriction()` is not active, `extractBytes32Array(words, offset)` will attempt to allocate/iterate past the bounds of `words`, throwing an unhandled exception during precompile execution triggered by a single crafted `TriggerSmartContract` transaction.

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1165-1171)
```java
      if (VMConfig.allowTvmSelfdestructRestriction()) {
        int sigArraySize = words[words[1].intValueSafe() / WORD_SIZE].intValueSafe();
        int addrArraySize = words[words[2].intValueSafe() / WORD_SIZE].intValueSafe();
        if (sigArraySize > MAX_SIZE || addrArraySize > MAX_SIZE) {
          return Pair.of(true, DATA_FALSE);
        }
      }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1176-1177)
```java
      byte[][] addresses = extractBytes32Array(
          words, words[2].intValueSafe() / WORD_SIZE);
```
