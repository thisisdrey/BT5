### Title
Unbounded array allocation via unchecked attacker-controlled length field in TVM precompiled contracts `BatchValidateSign`/`ValidateMultiSign` - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
`extractBytesArray`, `extractBytes32Array`, and `extractSigArray` in `PrecompiledContracts.java` read an array-length field directly from attacker-controlled TVM call data (`words[offset].intValueSafe()`) and use it, unvalidated against the size of the actual `words`/`data` buffers, to size a new array (`new byte[len][]`) and to drive indexing loops. This mirrors the `sc_device_msg_deserialize()` bug class in the external report: a length value taken from untrusted input is trusted to bound subsequent memory operations without verifying it against the real buffer size. [1](#0-0) 

### Finding Description
`extractBytes32Array` has no bound check at all before indexing into `words`:
```java
private static byte[][] extractBytes32Array(DataWord[] words, int offset) {
  int len = words[offset].intValueSafe();
  byte[][] bytes32Array = new byte[len][];
  for (int i = 0; i < len; i++) {
    bytes32Array[i] = words[offset + i + 1].getData();
  }
  return bytes32Array;
}
``` [2](#0-1) 

`extractBytesArray` only guards `offset > words.length - 1`, but `len` (also attacker-controlled) is unbounded and used both to allocate `new byte[len][]` and to derive `bytesOffset`/`bytesLen` fed into `extractBytes(data, ...)`: [3](#0-2) 

These helpers are called from the `BatchValidateSign` (address `0x...09`) and `ValidateMultiSign` (address `0x...0a`) precompiled contracts, both reachable from any unprivileged smart contract via a `CALL`/`STATICCALL` to the precompile address: [4](#0-3) [5](#0-4) 

Critically, the length-bounding check (`sigArraySize > MAX_SIZE || addrArraySize > MAX_SIZE`) is only applied when `VMConfig.allowTvmSelfdestructRestriction()` is enabled; when that flag is not active, `extractBytesArray`/`extractBytes32Array`/`extractSigArray` are invoked with the raw, attacker-supplied `len` with **no upper bound at all**: [6](#0-5) 

Energy accounting for these precompiles is computed purely from the physical `data.length` of the call, not from the declared `len` field inside the ABI-encoded array:
```java
public long getEnergyForData(byte[] data) {
  long cnt = (data.length / WORD_SIZE - 5) / 6;
  return cnt * ENGERYPERSIGN;
}
``` [7](#0-6) 

This lets an attacker submit a minimal-size call (few words, low energy charge) that encodes a huge declared array length (e.g. `0x7FFFFFFF`) inside one of those words, causing `new byte[len][]` to attempt an allocation of billions of object-reference slots for negligible energy cost.

### Impact Explanation
Any unprivileged account can trigger this by calling a smart contract that performs a single `CALL`/`STATICCALL` to precompiled address `0x...09` (`BatchValidateSign`) or `0x...0a` (`ValidateMultiSign`) with crafted call data. The declared array length is fully attacker-controlled and disconnected from the energy cost paid, so a cheap transaction can force the node's JVM to attempt an oversized array allocation (`new byte[Integer.MAX_VALUE][]`), which can throw `OutOfMemoryError`/exhaust heap and threaten to crash or destabilize the node process that serves all other transactions and API requests — a node-crash / node-halt class impact reachable from a single signed transaction.

### Likelihood Explanation
Likelihood is high: the vulnerable code paths (`BatchValidateSign`, `ValidateMultiSign`) are reachable by any account able to broadcast a transaction that calls a contract invoking these precompiles — no special permission, staking, or node cooperation required. The bypass of the size guard is trivial: it only requires the `allowTvmSelfdestructRestriction` hard-fork flag to be disabled (older or partially-activated networks), or triggering `extractBytes32Array` (which is never size-checked in the non-restricted call path for `addresses`) directly.

### Recommendation
- Enforce the `MAX_SIZE` (and a hard upper cap) on the declared `len` value in `extractBytesArray`, `extractBytes32Array`, and `extractSigArray` unconditionally, not only when `VMConfig.allowTvmSelfdestructRestriction()` is active.
- Validate `len`, `offset`, and derived `bytesOffset`/`bytesLen` against the bounds of `words`/`data` before allocating or indexing, returning a rejection (`Pair.of(false, EMPTY_BYTE_ARRAY)`) instead of throwing/allocating on malformed input.
- Tie `getEnergyForData` to the *declared* array length (as parsed from call data), not merely the raw call data size, so cost cannot be decoupled from actual work/memory requested.

### Proof of Concept
1. Deploy/call a contract that issues `STATICCALL` to precompile address `0x...0a` (`ValidateMultiSign`) or `0x...09` (`BatchValidateSign`) on a network where `allowTvmSelfdestructRestriction` is disabled.
2. Construct ABI call data with the minimal header words (5 words) plus one extra data word, but set the word at the offset referenced by `words[3]` (for `ValidateMultiSign`) — the array-length slot — to `0x7FFFFFFF` instead of a small real count.
3. `execute()` invokes `extractBytesArray(words, offset, data)` (or `extractBytes32Array`), which executes `new byte[0x7FFFFFFF][]`, attempting a multi-gigabyte allocation for a call that only pays energy proportional to the small physical `data.length`, risking `OutOfMemoryError`/node instability.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L390-412)
```java
  private static byte[][] extractBytes32Array(DataWord[] words, int offset) {
    int len = words[offset].intValueSafe();
    byte[][] bytes32Array = new byte[len][];
    for (int i = 0; i < len; i++) {
      bytes32Array[i] = words[offset + i + 1].getData();
    }
    return bytes32Array;
  }

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1051-1075)
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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1137-1142)
```java
    @Override
    public long getEnergyForData(byte[] data) {
      long cnt = (data.length / WORD_SIZE - 5) / 6;
      // one sign 1500, half of ecrecover
      return cnt * ENGERYPERSIGN;
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1156-1181)
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
      int cnt = signatures.length;
      if (cnt == 0 || cnt > MAX_SIZE || signatures.length != addresses.length) {
        return Pair.of(true, DATA_FALSE);
      }
```
