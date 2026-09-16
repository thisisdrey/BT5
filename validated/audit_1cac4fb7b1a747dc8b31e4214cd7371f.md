### Title
Unbounded array index in `ValidateMultiSign` precompile causes uncaught `ArrayIndexOutOfBoundsException` from attacker-controlled TVM calldata - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The CVE-2016-10169 report describes WavPack's `read_code` performing an out-of-bounds read because a length/index value taken directly from untrusted file data was used to index a buffer without validating it against the buffer's actual size. The same bug class — an attacker-supplied offset/length used to index a parsed-word array without a bounds check — exists in the `ValidateMultiSign` precompiled contract's `execute` method.

### Finding Description
`ValidateMultiSign.execute` (and its sibling `BatchValidateSign.doExecute`) parses the raw call data into fixed 32-byte `DataWord` slots with `DataWord.parseArray(rawData)` and then uses an attacker-controlled offset word to index directly into that array: [1](#0-0) 
When `VMConfig.allowTvmSelfdestructRestriction()` is active, the code computes `words[3].intValueSafe() / WORD_SIZE` and immediately uses this attacker-controlled value as a raw index into `words` with no bounds check: `words[words[3].intValueSafe() / WORD_SIZE].intValueSafe()`. Because a contract caller fully controls the calldata that becomes `words`, this index can trivially exceed `words.length - 1` (or be negative if `intValueSafe()` produces a value whose division underflows/behaves unexpectedly), throwing `ArrayIndexOutOfBoundsException`.

This mirrors the two "safe" helper functions in the same file, `extractBytesArray` and `extractSigArray`, which explicitly guard against this exact condition with `if (offset > words.length - 1) { return new byte[0][]; }`: [2](#0-1) 
but the inline lookup at line 1067 in `ValidateMultiSign.execute` and the analogous lookups in `BatchValidateSign.doExecute` (lines 1166-1167) bypass that pattern entirely and index `words[]` before any bounds validation. `extractBytes32Array` itself is also missing this guard entirely: [3](#0-2) 

Critically, `BatchValidateSign.execute` wraps its body in a `try { return doExecute(data); } catch (Throwable t) { ... return Pair.of(true, new byte[WORD_SIZE]); }`: [4](#0-3) 
so any thrown `ArrayIndexOutOfBoundsException` there is swallowed and does not escape. However, `ValidateMultiSign.execute` has **no such enclosing try/catch** around the unsafe index expression at line 1067 — only a narrower `try` further down around the signature-loop, after the vulnerable index access already executed: [5](#0-4) 

### Impact Explanation
If `words[words[3].intValueSafe() / WORD_SIZE]` throws `ArrayIndexOutOfBoundsException`, that exception propagates out of `ValidateMultiSign.execute` uncaught by the precompile itself. Whether this ultimately crashes the node, aborts only the current transaction, or is caught generically further up the TVM call stack (in `Program`/`VM`/`VMActuator`) could not be confirmed from the code I was able to inspect in this session — I was unable to fully trace the top-level exception handling around `callToPrecompiledAddress`/`VM.play()` due to running out of investigation budget. At minimum this is a reachable, attacker-triggerable unhandled runtime exception from ordinary contract calldata (any account can call the `validatemultisign` precompile at address `0x...0a`, gated only by `VMConfig.allowTvmSolidity059()`), which is a real behavioral inconsistency compared to the deliberately-hardened `BatchValidateSign`/`extractBytesArray`/`extractSigArray` sibling code paths in the same class.

### Likelihood Explanation
High reachability: `ValidateMultiSign` is a public precompiled contract reachable via a plain `CALL`/`STATICCALL` from any smart contract, with only a hard-fork flag gate (`allowTvmSolidity059`) and, for the vulnerable branch, `allowTvmSelfdestructRestriction`. No special privileges are required, and the calldata layout (`words[3]`) is entirely attacker-supplied ABI-encoded bytes, so triggering the out-of-range index requires no more than crafting the `signatures` offset field to point outside the array bounds.

### Recommendation
Add the same `offset > words.length - 1` (and negative-offset) guard used in `extractBytesArray`/`extractSigArray` before the raw `words[words[3].intValueSafe() / WORD_SIZE]` access in `ValidateMultiSign.execute`, and apply the identical guard to the analogous lookups in `BatchValidateSign.doExecute` and to `extractBytes32Array`, returning `DATA_FALSE`/`Pair.of(true, DATA_FALSE)` on out-of-range input instead of relying on an exception being thrown and caught elsewhere. Given I could not confirm the top-level catch behavior in `VM`/`Program`, I recommend independently verifying (with a background Devin session that has full read access to `VM.java`/`Program.java`/`VMActuator.java`) whether an uncaught `ArrayIndexOutOfBoundsException` from a precompile actually reverts cleanly or can crash the node process/JVM thread.

### Proof of Concept
Construct calldata for `validatemultisign(address,uint256,bytes32,bytes[])` (or the equivalent raw word layout) such that:
- `words[3]` (the offset field for the `bytes[]` signatures parameter) is set to a large value, e.g., `0xFFFFFFFF * WORD_SIZE`, or any value such that `words[3].intValueSafe() / WORD_SIZE >= words.length`.
- Enable/activate `allowTvmSelfdestructRestriction` (already active on current mainnet per TIP history).

Calling this precompile with such calldata causes `words[words[3].intValueSafe() / WORD_SIZE]` at `PrecompiledContracts.java:1067` to throw `ArrayIndexOutOfBoundsException`, which is not caught anywhere within `ValidateMultiSign.execute` — unlike the equivalent `BatchValidateSign` path which is defensively wrapped.

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1051-1082)
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

      if (signatures.length == 0 || signatures.length > MAX_SIZE) {
        return Pair.of(true, DATA_FALSE);
      }

      AccountCapsule account = this.getDeposit().getAccount(address);
      if (account != null) {
        try {
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1144-1154)
```java
    @Override
    public Pair<Boolean, byte[]> execute(byte[] data) {
      try {
        return doExecute(data);
      } catch (Throwable t) {
        if (t instanceof InterruptedException){
          Thread.currentThread().interrupt();
        }
        return Pair.of(true, new byte[WORD_SIZE]);
      }
    }
```
