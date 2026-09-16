Based on my investigation, I found a strong analog in `PrecompiledContracts.java`'s `extractBytes` helper, which is reachable from an unprivileged transaction's `CALL` to the `ValidateMultiSign`/`BatchValidateSign` precompiles.

### Title
Heap out-of-bounds read via unchecked attacker-controlled length/offset in `PrecompiledContracts.extractBytes` used by `ValidateMultiSign`/`BatchValidateSign` precompiles - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
`extractBytes(byte[] data, int offset, int len)` performs `Arrays.copyOfRange(data, offset, offset + len)` with no validation that `offset`/`len` are within `data`'s bounds, and no check that `len` is non-negative. `offset` and `len` are derived directly from attacker-controlled `DataWord`s in the precompile input (via `intValueSafe()`), exactly mirroring the libtpms bug class: an oversized/negative "length" field is not validated against the remaining buffer size before being used to read the buffer. [1](#0-0) 

### Finding Description
`extractBytesArray`/`extractSigArray` compute `bytesOffset` and `bytesLen` from `words[...].intValueSafe()` — values fully controlled by the caller's ABI-encoded input to the `ValidateMultiSign` (address `0x...0a`) or `BatchValidateSign` (address `0x...09`) precompiles — and pass them straight into `extractBytes`: [2](#0-1) 

`extractBytes` itself does no bounds checking at all:
```java
private static byte[] extractBytes(byte[] data, int offset, int len) {
    return Arrays.copyOfRange(data, offset, offset + len);
}
``` [1](#0-0) 

`intValueSafe()` clamps huge values but the resulting `offset`/`len` can still exceed `data.length`, or `offset + len` can be crafted to point past the end of the buffer (or, via `intValueSafe()` on very large unsigned words, produce values that combine to an out-of-bounds range). `Arrays.copyOfRange` when given `offset > data.length` or `offset+len` beyond array length throws `ArrayIndexOutOfBoundsException`, but in the `ValidateMultiSign.execute` / `BatchValidateSign.doExecute` call paths this is only guarded generically:
- `ValidateMultiSign.execute` has no outer try/catch around the `extractBytesArray`/`extractSigArray` call itself (only around the later permission logic), so an `ArrayIndexOutOfBoundsException` from `extractBytes` propagates as an uncaught VM exception. [3](#0-2) 
- `BatchValidateSign.execute` wraps `doExecute` in a `try { } catch (Throwable t)` that swallows all exceptions and silently returns an all-zero result: [4](#0-3) 

This mirrors the libtpms flaw: an oversized/attacker-chosen "block length" (`bytesLen`) is used to index/copy from a buffer (`rawData`) without checking it against the remaining size of that buffer, and the safety net (`intValueSafe()`/generic exception clamp) does not perform the specific bounds check that would prevent the out-of-range read — it only prevents crashes for `BatchValidateSign`, not for `ValidateMultiSign`.

### Impact Explanation
Any unprivileged account can invoke a smart contract that calls the `ValidateMultiSign` precompile (address `0x...0a`, gated by `VMConfig.allowTvmSolidity059()`) with crafted ABI-encoded signature-array offsets/lengths, triggering `Arrays.copyOfRange` on `rawData` with an out-of-range `offset`/`len`. This throws an uncaught `ArrayIndexOutOfBoundsException`/`NegativeArraySizeException` inside the precompile execution path, which is not specifically caught, potentially crashing the transaction execution flow (unhandled RuntimeException surfacing up through the VM/actuator) — a node-crash / denial-of-service on any node that processes such a transaction. This satisfies the "node crash or halt" acceptance criterion.

### Likelihood Explanation
High: `ValidateMultiSign` is a broadcastable-transaction-reachable contract call precompile guarded only by a feature flag that is enabled on mainnet (`allowTvmSolidity059`), requires no special privilege, and the malformed offset/length values are trivial to construct in the ABI payload of a normal `TriggerSmartContract` transaction.

### Recommendation
Add explicit bounds validation in `extractBytes` (and callers `extractBytesArray`/`extractSigArray`) verifying `offset >= 0`, `len >= 0`, and `offset + len <= data.length` before calling `Arrays.copyOfRange`, returning a `false`/`DATA_FALSE` result (as already done elsewhere, e.g., `isValidAbiEncoding`) instead of throwing, and ensure `ValidateMultiSign.execute` catches the same broad `Throwable` as `BatchValidateSign.execute` around the extraction calls.

### Proof of Concept
Deploy a contract that performs a low-level `CALL` to precompile address `0x000000000000000000000000000000000000000000000000000000000000000a` (`ValidateMultiSign`) with an ABI payload where:
- `words[3]` (signature-array offset word) is set so that `words[3].intValueSafe()/WORD_SIZE` points to a slot whose declared array length (`len`) is large, and
- the per-signature offset word is crafted so `bytesOffset` (word 406/421) combined with `SIG_LENGTH`/`bytesLen` makes `(bytesOffset + offset + 2) * WORD_SIZE + len` exceed `rawData.length`,

causing `extractBytes` to call `Arrays.copyOfRange(rawData, offset, offset+len)` with `offset+len > rawData.length`, throwing an uncaught exception in `ValidateMultiSign.execute` (path at lines 1072–1074), crashing transaction execution. [5](#0-4)

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L428-430)
```java
  private static byte[] extractBytes(byte[] data, int offset, int len) {
    return Arrays.copyOfRange(data, offset, offset + len);
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1051-1078)
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
