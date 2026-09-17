### Title
Unbounded attacker-controlled offsets in `extractBytesArray`/`extractSigArray`/`extractBytes32Array` cause out-of-bounds array reads in `ValidateMultiSign`/`BatchValidateSign` precompiles - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
The `ValidateMultiSign` and `BatchValidateSign` TVM precompiled contracts decode ABI-encoded input into a `DataWord[]` and then use attacker-supplied length/offset words as direct array indices into both the `words[]` array and the raw `data`/`rawData` byte array, without validating those indices against the actual array bounds before use.

### Finding Description
`extractBytesArray` and `extractSigArray` only perform a shallow bound check (`offset > words.length - 1`) before reading `len = words[offset].intValueSafe()` and then looping `i` from `0` to `len`, dereferencing `words[offset + i + 1]` and computing `bytesOffset`/`bytesLen` from further attacker-controlled words: [1](#0-0) 
None of `offset + i + 1`, `bytesOffset + offset + 2`, or the resulting `extractBytes` call range are checked against `words.length` or `data.length`: [2](#0-1) 
This is directly analogous to the TimescaleDB bug class: a value carried in a narrow/derived index field (there, a signed int16 Arrow dictionary index; here, a `DataWord.intValueSafe()`-derived offset/length) is used to index into a backing buffer after only a superficial validation check, allowing the true bounds check to be bypassed and an out-of-bounds read to occur.

These helpers are reached directly from the `ValidateMultiSign` and `BatchValidateSign` precompile `execute()` methods, both of which are callable by any contract (or any account triggering a contract call) via a normal CALL to the fixed precompile addresses, with attacker-fully-controlled `rawData`/`data`: [3](#0-2) [4](#0-3) 
Note the calls to `extractSigArray`/`extractBytesArray`/`extractBytes32Array` occur *before* any `try`/catch wrapping (the `try` block in `ValidateMultiSign.execute` starts only around the signature-recovery loop), so an `ArrayIndexOutOfBoundsException` or `NegativeArraySizeException` thrown during offset/length extraction is not swallowed at that level.

### Impact Explanation
A crafted `words[3]` (or `words[1]`/`words[2]` for `BatchValidateSign`) value can make `offset`, `bytesOffset`, or `bytesLen` point past the end of the decoded `words[]` array or past `rawData`/`data`, triggering an unhandled `ArrayIndexOutOfBoundsException`/`NegativeArraySizeException` inside the precompile logic. Depending on how far up the call stack this propagates before being caught (whether it is caught generically as a VM `Exception`/`Throwable` and turned into a revert, or propagates further), this can manifest as an uncontrolled exception affecting transaction execution rather than a memory-safety issue (Java bounds-checks arrays, so there is no true OOB memory corruption as in the native TimescaleDB C code) — the concrete, reachable impact here is a query/transaction-processing failure (denial-of-service class) triggerable by any account submitting a transaction that calls these precompiles, mirroring the "authenticated attacker triggers OOB via index validation bypass" pattern from the report, but bounded by JVM array-bounds enforcement rather than raw memory access.

### Likelihood Explanation
High reachability: the precompiles are invoked via ordinary CALL from any deployed contract, requiring no special privilege beyond broadcasting a transaction that triggers the call — matching the "authenticated/any-DML" reachability criterion from the report. The `VMConfig.allowTvmSelfdestructRestriction()` size checks (`sigArraySize > MAX_SIZE`) only bound the reported array-length word itself, not the intermediate `bytesOffset`/`bytesLen` derived indices used before or independent of that check, so a value crafted to fail the shallow `offset > words.length - 1` guard while still causing an out-of-range access deeper in the loop is plausible.

### Recommendation
Add explicit bounds checks in `extractBytesArray`, `extractSigArray`, and `extractBytes32Array` (and in `extractBytes`) validating that `offset + i + 1 < words.length`, that `bytesOffset + offset + 2` and `bytesOffset + offset + 2) * WORD_SIZE + bytesLen` stay within `data.length`, and that `bytesLen`/`len` are non-negative and within a sane upper bound, returning a failure (`Pair.of(false, ...)` or `DATA_FALSE`) instead of allowing an uncaught exception to escape the precompile execution path.

### Proof of Concept
Construct a call to the `ValidateMultiSign` precompile (address `0x...09` conceptually, per `PrecompiledContracts` registration) with `rawData` such that word[3] (the signature-array offset word) decodes to a small in-bounds value causing `extractBytesArray`'s `offset > words.length - 1` guard to pass, but where the derived `bytesOffset` (from `words[offset + i + 1].intValueSafe() / WORD_SIZE`) is crafted to reference a `words` index beyond the true `words.length`, or where the resulting `(bytesOffset + offset + 2) * WORD_SIZE` offset exceeds `rawData.length`, causing `extractBytes`'s `Arrays.copyOfRange` to throw. Submit this as a `TriggerSmartContract` calling the precompile via a helper contract's assembly `call` — no special permission beyond normal contract invocation is required.

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L414-430)
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

  private static byte[] extractBytes(byte[] data, int offset, int len) {
    return Arrays.copyOfRange(data, offset, offset + len);
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1051-1074)
```java
    @Override
    public Pair<Boolean, byte[]> execute(byte[] rawData) {
      if (VMConfig.allowTvmOsaka()
          && !isValidAbiEncoding(rawData, ABI_HEADER_WORDS, ABI_ITEM_WORDS)) {
        return Pair.of(false, EMPTY_BYTE_ARRAY);
      }
      DataWord[] words = DataWord.parseArray(rawData);
      byte[] address = words[0].toTronAddress();
      int permissionId = words[1].intValueSafe();
      byte[] data = words[2].getData();

      byte[] combine = ByteUtil.merge(address, ByteArray.fromInt(permissionId), data);
      byte[] hash = Sha256Hash.hash(CommonParameter
          .getInstance().isECKeyCryptoEngine(), combine);

      if (VMConfig.allowTvmSelfdestructRestriction()) {
        int sigArraySize = words[words[3].intValueSafe() / WORD_SIZE].intValueSafe();
        if (sigArraySize > MAX_SIZE) {
          return Pair.of(true, DATA_FALSE);
        }
      }
      byte[][] signatures = VMConfig.allowTvmSelfdestructRestriction() ?
          extractSigArray(words, words[3].intValueSafe() / WORD_SIZE, rawData) :
          extractBytesArray(words, words[3].intValueSafe() / WORD_SIZE, rawData);
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1156-1177)
```java
    private Pair<Boolean, byte[]> doExecute(byte[] data)
        throws InterruptedException, ExecutionException {
      if (VMConfig.allowTvmOsaka()
          && !isValidAbiEncoding(data, ABI_HEADER_WORDS, ABI_ITEM_WORDS)) {
        return Pair.of(false, EMPTY_BYTE_ARRAY);
      }
      DataWord[] words = DataWord.parseArray(data);
      byte[] hash = words[0].getData();

      if (VMConfig.allowTvmSelfdestructRestriction()) {
        int sigArraySize = words[words[1].intValueSafe() / WORD_SIZE].intValueSafe();
        int addrArraySize = words[words[2].intValueSafe() / WORD_SIZE].intValueSafe();
        if (sigArraySize > MAX_SIZE || addrArraySize > MAX_SIZE) {
          return Pair.of(true, DATA_FALSE);
        }
      }

      byte[][] signatures = VMConfig.allowTvmSelfdestructRestriction() ?
          extractSigArray(words, words[1].intValueSafe() / WORD_SIZE, data) :
          extractBytesArray(words, words[1].intValueSafe() / WORD_SIZE, data);
      byte[][] addresses = extractBytes32Array(
          words, words[2].intValueSafe() / WORD_SIZE);
```
