### Title
Heap-buffer-over-read analog: unchecked attacker-controlled offsets in `ValidateMultiSign` precompile's array extraction helpers cause `ArrayIndexOutOfBoundsException` / crafted out-of-bounds reads - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
CVE-2017-6969 is a heap-based buffer over-read in `readelf` caused by trusting length/offset fields taken from a corrupt binary without validating them against the actual buffer size. The `ValidateMultiSign` precompiled contract in java-tron exhibits the same root-cause pattern: it decodes attacker-supplied ABI-encoded call data into a `DataWord[]` array and then uses length/offset values taken directly from that data to index into the array and into the raw byte buffer, with several of the helper methods it calls performing no (or only partial) bounds checking.

### Finding Description
`ValidateMultiSign.execute()` at [1](#0-0)  parses caller-supplied precompile input (`rawData`) into `DataWord[] words = DataWord.parseArray(rawData)` and then computes an offset from attacker-controlled data: `words[3].intValueSafe() / WORD_SIZE`. This computed offset is passed to `extractSigArray` or `extractBytesArray`.

`extractBytes32Array` performs **no bounds check at all** on `offset` before indexing `words[offset]`: [2](#0-1) 

`extractBytesArray` and `extractSigArray` only check that the initial `offset` itself is within `words.length`, but do not validate the per-item `bytesOffset` (also attacker-derived from `words[offset + i + 1].intValueSafe() / WORD_SIZE`) or the resulting index `offset + bytesOffset + 1` before it is used to index into `words`, nor does it validate that `(bytesOffset + offset + 2) * WORD_SIZE` plus `bytesLen`/`SIG_LENGTH` stays within the bounds of the raw `data` byte array passed to `extractBytes`: [3](#0-2) 

`extractBytes` itself directly calls `Arrays.copyOfRange(data, offset, offset + len)` with no validation: [4](#0-3) 

Because `offset`, `bytesOffset`, and `bytesLen`/length values used in `extractBytesArray`/`extractSigArray`/`extractBytes32Array` come straight from `words[...].intValueSafe()` — a value fully controlled by the transaction/contract call data — a crafted call can drive these indices past the end of the `words` array or the `data` byte array. This mirrors the readelf bug class: a "length/offset" field parsed from untrusted input is trusted without being checked against the real buffer size, leading to an out-of-bounds read (in Java, this manifests as `ArrayIndexOutOfBoundsException` / `NegativeArraySizeException`, not silent memory disclosure, but it is the same missing-validation root cause).

Note: `isValidAbiEncoding` is only invoked when `VMConfig.allowTvmOsaka()` is enabled (see line 1053-1056), and even that check only validates that `data.length` is a multiple of `WORD_SIZE` and the tail size relationship — it does not validate the specific offsets derived from `words[3]` used later, so the protection is incomplete even when enabled.

### Impact Explanation
An unprivileged contract caller can invoke the `ValidateMultiSign` precompile (reachable via a normal `CALL`/`STATICCALL` from any smart contract, itself invokable by any signed transaction) with a crafted `data` payload that sets `words[3]` (and subsequent length/offset words) to out-of-range values. This throws an unhandled `ArrayIndexOutOfBoundsException` or `NegativeArraySizeException` inside the TVM execution path. Depending on how the surrounding `VMActuator`/`Program` exception handling treats uncaught runtime exceptions from precompiled contracts (vs. the deliberate `Program.Exception` types), this can propagate as an unexpected node-level runtime error during transaction execution, causing inconsistent behavior/crash risk for full nodes processing the same transaction, and in the worst case a difference in how nodes handle the exception could create a consensus/chain-split risk. At minimum it is a reliable, remotely triggerable crash/DoS-style logic fault in a core VM precompile.

### Likelihood Explanation
High: `ValidateMultiSign` is a standard precompiled contract reachable by any unprivileged party through a plain smart-contract call; no special permissions, staking, or witness/SR status is required, only a signed transaction and a contract that calls the precompile address. The vulnerable helper functions are on the direct execution path with no upstream validation of the derived offsets (`isValidAbiEncoding` does not check these specific indices, and is only active behind an Osaka feature flag).

### Recommendation
Add explicit bounds checks in `extractBytes32Array`, `extractBytesArray`, and `extractSigArray` before every array index derived from attacker-controlled `DataWord` values (validate `offset`, `bytesOffset`, and the final byte-range against both `words.length` and `data.length`), and in `extractBytes` validate `offset >= 0 && offset + len <= data.length` before calling `Arrays.copyOfRange`. Wrap `ValidateMultiSign.execute` (and similarly-shaped precompiles) so any unexpected `RuntimeException` from these helpers is converted to a well-defined `Pair.of(false, EMPTY_BYTE_ARRAY)` failure instead of propagating as an uncaught exception.

### Proof of Concept
Construct precompile input for `ValidateMultiSign` such that:
- `words[0]` = arbitrary 20-byte address, `words[1]` = permission id, `words[2]` = signed data.
- `words[3]` = a huge value (e.g. `0x7fffffff * WORD_SIZE`) so that `words[3].intValueSafe() / WORD_SIZE` yields an offset far beyond `words.length`.

Then call the precompile address for `ValidateMultiSign` via `CALL` from any deployed contract with this crafted calldata. `extractBytesArray`/`extractSigArray` will index `words[offset + i + 1]` (or `extractBytes32Array` will index `words[offset]` directly, if reached via another code path), throwing `ArrayIndexOutOfBoundsException` inside `execute()` at [5](#0-4) , an unhandled runtime exception surfacing from the precompiled-contract execution path.

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L399-430)
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

  private static byte[] extractBytes(byte[] data, int offset, int len) {
    return Arrays.copyOfRange(data, offset, offset + len);
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1052-1074)
```java
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
