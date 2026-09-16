### Title
Unbounded array allocation from attacker-controlled length word in TVM precompile signature-array parsing causes Out-Of-Memory node crash - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
Like CVE-2017-11551, where `id3_field_parse` in libid3tag trusted a length field from an untrusted MP3 file and allocated memory accordingly without bounding it (causing OOM), java-tron's precompiled-contract helper routines `extractBytesArray` and `extractSigArray` in `PrecompiledContracts.java` read a 32-byte length word directly out of attacker-supplied EVM calldata and immediately allocate a Java array sized by that value, with no upper-bound check before the allocation.

### Finding Description
`extractBytesArray` and `extractSigArray` decode the number of array elements from calldata words and allocate a reference array of that length before any validation of a sane maximum: [1](#0-0) [2](#0-1) 

Both helpers call `words[offset].intValueSafe()` to obtain `len`, then execute `new byte[len][]` immediately. Unlike `ModExp.execute`, which explicitly caps `baseLen`/`expLen`/`modLen` against an `UPPER_BOUND` constant before touching memory: [3](#0-2) 
there is no analogous bound applied in `extractBytesArray`/`extractSigArray` before the array allocation. A crafted `len` close to `Integer.MAX_VALUE` forces the JVM to attempt to allocate a reference array of that many slots (≈8GB for a `byte[][]` on a 64-bit JVM with compressed oops, more without), which throws `OutOfMemoryError` in the JVM process that executes the contract, and depending on GC pressure can degrade or crash the node process handling that transaction — directly analogous to the id3tag crafted-length OOM.

These helpers are used by the precompiled signature-verification contracts (`BatchValidateSign` / `ValidateMultiSign`), reachable from any Solidity contract issuing a `CALL`/`STATICCALL` to the corresponding precompile address, meaning any account able to deploy or trigger a smart contract (an unprivileged transaction broadcaster) can reach this code path with fully attacker-controlled calldata.

### Impact Explanation
An `OutOfMemoryError` thrown inside a single JVM node process is not automatically contained to the triggering transaction; depending on the surrounding exception handling and GC state, it can destabilize the whole node process (denial of service), preventing the node from continuing to serve transactions/API requests until restarted. This matches the "node crash or halt" impact bucket in the validation criteria.

### Likelihood Explanation
Any account can deploy a trivial contract that performs a `staticcall`/`call` to the BatchValidateSign/ValidateMultiSign precompile address with a crafted calldata word setting the array-length field to a very large value; this requires no special privilege, only enough energy/fee to submit the triggering transaction, making the likelihood high once such a call path is confirmed to be reachable at the given precompile address.

### Recommendation
Add an explicit upper bound check on `len` (and on subsequently derived `bytesLen`) in `extractBytesArray` and `extractSigArray`, mirroring the `UPPER_BOUND` pattern already used in `ModExp`, and reject (return failure) rather than allocate when the requested length exceeds the maximum number of legitimately expected signatures/addresses for the precompile.

### Proof of Concept
Deploy a contract that calls the BatchValidateSign/ValidateMultiSign precompile address with calldata whose "array length" word (the word consumed by `words[offset].intValueSafe()` inside `extractBytesArray`/`extractSigArray`) is set to a very large value (e.g., `0x7FFFFFFF`); submitting this as a normal `TriggerSmartContract` transaction drives execution into `new byte[len][]`, triggering an `OutOfMemoryError` in the executing node's JVM.

**Note on confidence:** I was not able to fully trace the exact `execute()` bodies of `BatchValidateSign`/`ValidateMultiSign` that invoke `extractBytesArray`/`extractSigArray` within the available context (the file is large and the specific call sites for these two helper methods weren't fully visible in the retrieved excerpts, only their definitions). The precompile-address reachability and the specific attacker-controlled offset/length wiring should be double-checked against the full `BatchValidateSign`/`ValidateMultiSign` `execute()` implementations before treating this as fully confirmed.

### Citations

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L686-699)
```java
    @Override
    public Pair<Boolean, byte[]> execute(byte[] data) {

      if (data == null) {
        return Pair.of(true, EMPTY_BYTE_ARRAY);
      }

      int baseLen = parseLen(data, 0);
      int expLen = parseLen(data, 1);
      int modLen = parseLen(data, 2);

      if (VMConfig.allowTvmOsaka()
          && (baseLen > UPPER_BOUND || expLen > UPPER_BOUND || modLen > UPPER_BOUND)) {
        return Pair.of(false, EMPTY_BYTE_ARRAY);
```
