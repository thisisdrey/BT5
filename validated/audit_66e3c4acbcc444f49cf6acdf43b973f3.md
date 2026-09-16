### Title
Unbounded array allocation from attacker-controlled length words in `ValidateMultiSign`/`BatchValidateSign` precompiles can OOM-crash a node - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The CometBFT block-sync bug class (an attacker-controlled numeric field driving unbounded memory allocation, causing crashes) has a direct analog reachable by any unprivileged contract deployer/caller: the `ValidateMultiSign` and `BatchValidateSign` TVM precompiled contracts parse a signature-count/address-count field directly from call data and use it to size a Java array *before* any bound is unconditionally enforced.

### Finding Description
`extractBytesArray` and `extractBytes32Array` read a length word straight from attacker-supplied calldata and immediately allocate an array of that size: [1](#0-0) 

`words[offset].intValueSafe()` can return a value up to `Integer.MAX_VALUE` under attacker control, and `new byte[len][]` is allocated with no upper-bound check in this code path.

In `ValidateMultiSign.execute`, the `MAX_SIZE` (5) bound on `sigArraySize` is only enforced when the `allowTvmSelfdestructRestriction()` proposal flag is active; when it is not, `extractBytesArray` is invoked directly with the unchecked, attacker-supplied length: [2](#0-1) 

Similarly, in `BatchValidateSign.doExecute`, the `MAX_SIZE` (16) check on `sigArraySize`/`addrArraySize` is gated behind the same flag, but `extractBytes32Array` (which sizes `addresses` from `addrArraySize`) is always called afterward regardless of whether the flag-gated check ran: [3](#0-2) 

This mirrors the reported CometBFT class of bug: a single field taken from untrusted input (there, `LastCommit` round; here, an ABI-encoded array-length word) is used to size a memory structure with no independent sanity check on the allocation path, enabling an out-of-memory condition.

### Impact Explanation
Any account can deploy or call a contract that invokes the `ValidateMultiSign` (address `0x1000001`) or `BatchValidateSign` (address `0x1000002`) precompile with a crafted length word requesting an extremely large array. Because every full node executing/validating the block must run the same TVM code deterministically, a single malicious transaction propagated to the network can trigger `OutOfMemoryError`/excessive GC pressure on every node that processes it, potentially crashing nodes or halting block processing — a network-wide denial of service, not merely resource exhaustion on the sender.

### Likelihood Explanation
Exploitation only requires a normal signed transaction calling a smart contract that triggers the precompile with attacker-chosen ABI-encoded parameters — no special privileges, validator/committee role, or peer-level access is needed. Whether this is currently exploitable in production depends on whether the `allowTvmSelfdestructRestriction` proposal has been activated network-wide (which caps the affected sizes at `MAX_SIZE`); this activation status could not be confirmed from the code alone, so likelihood is assessed as moderate pending that confirmation.

### Recommendation
Enforce the `MAX_SIZE` bound (or an equivalent sane upper limit) unconditionally in `extractBytesArray`, `extractBytes32Array`, and `extractSigArray`, independent of the `allowTvmSelfdestructRestriction` feature flag, and validate the parsed length against `MAX_SIZE`/data bounds before allocating any array.

### Proof of Concept
1. Deploy a contract that performs a low-level `CALL` (or use a raw `TriggerSmartContract`) to precompiled contract address `0x0000000000000000000000000000000000000000000000000000000001000001` (`ValidateMultiSign`) with ABI-encoded data where the signature-array length word (at the offset pointed to by the 4th parameter) is set to a very large value (e.g., `0x7fffffff`).
2. If the `allowTvmSelfdestructRestriction()` proposal is not active on the target network, `ValidateMultiSign.execute` skips the `MAX_SIZE` pre-check and calls `extractBytesArray`, which executes `new byte[len][]` with `len` = the attacker-chosen value.
3. This throws `OutOfMemoryError`/consumes excessive heap during transaction execution on every node validating the block containing this transaction, matching the block-sync DoS pattern of unbounded allocation from an unchecked, attacker-controlled numeric field. [4](#0-3)

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1156-1179)
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
```
