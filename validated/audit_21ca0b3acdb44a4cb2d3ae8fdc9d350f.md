### Title
Unvalidated array-length word in `ValidateMultiSign`/`BatchValidateSign` precompile allows attacker to force huge array allocation before size checks — node DoS via crafted TVM call - ([File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java])

### Summary
The `extractBytesArray`, `extractSigArray`, and `extractBytes32Array` helper methods used by the `ValidateMultiSign` and `BatchValidateSign` precompiled contracts read an attacker-controlled 32-byte word from calldata as an array length (`int len = words[offset].intValueSafe();`) and immediately allocate `new byte[len][]` **before** any upper-bound (`MAX_SIZE`) check is performed. This mirrors the CVE-2017-7346 bug class: a "levels"/count field taken from untrusted input is used to size an internal structure without validating it against sane bounds first, letting an unprivileged caller trigger a denial of service.

### Finding Description
In `PrecompiledContracts.java`: [1](#0-0) 

`extractBytesArray` computes `len` purely from a caller-supplied `DataWord` and allocates `byte[len][]` with no upper bound check. The sibling helpers `extractSigArray` (lines 414-426) and `extractBytes32Array` (lines 390-397) do the same.

These helpers are invoked from `ValidateMultiSign.execute` and `BatchValidateSign.doExecute`: [2](#0-1) 

The `MAX_SIZE` bound check (`sigArraySize > MAX_SIZE`) is only performed when `VMConfig.allowTvmSelfdestructRestriction()` is enabled, and even then it is a separate check that duplicates reading `words[...]` — the actual array allocation via `extractBytesArray`/`extractSigArray` happens regardless and is not gated by the size guard when that hard-fork flag is disabled. Even when the flag is active, the check re-reads the same word but the unrestricted legacy path (`extractBytesArray`) is still reachable for callers before the corresponding hard fork, and `extractBytes32Array` (used for `addresses` in `BatchValidateSign`) has no bound check gated at all: [3](#0-2) 

The energy cost for these precompiles is derived from `data.length` (the raw calldata size), not from the declared array-length word: [4](#0-3) 

This decouples the energy charged from the actual allocation size requested by the length word — an attacker can craft a short calldata buffer (cheap in energy) that encodes a length word claiming an enormous number of elements (up to `Integer.MAX_VALUE`, since `intValueSafe()` clamps to `Integer.MAX_VALUE` rather than rejecting), causing `new byte[len][]` to attempt an allocation of billions of array-reference slots. This can throw `OutOfMemoryError`/hang the JVM heap for a fraction of the intended energy cost.

### Impact Explanation
Both `ValidateMultiSign` and `BatchValidateSign` are TVM precompiled contracts reachable by any contract that calls their fixed addresses (`0x...a` and `0x...9`) via a normal `TriggerSmartContract`. Any unprivileged transaction broadcaster/contract deployer can invoke them with attacker-chosen calldata. A crafted call with a fabricated huge length word forces the node executing the transaction (both the broadcaster's local execution during a full node's block-apply and any full node validating the block containing this call) to attempt a massive heap allocation, which can throw `OutOfMemoryError`, destabilize the JVM (GC thrashing, node hang) or crash the node process — a chain-wide DoS if such a transaction lands in a block and propagates to all full nodes.

### Likelihood Explanation
Likelihood is high: it requires only a single crafted transaction calling the `validatemultisign` or `batchvalidatesign` precompile with an oversized declared array-length word, no special privileges, no cooperating validator/committee member, and no prior on-chain state setup. The `intValueSafe()` conversion of an arbitrary 32-byte word to `int` permits a caller to set the length field near `Integer.MAX_VALUE`.

### Recommendation
Validate the declared length word against `MAX_SIZE` (or a hard sanity cap) in `extractBytesArray`, `extractSigArray`, and `extractBytes32Array` themselves, unconditionally (not just under the `allowTvmSelfdestructRestriction()` hard-fork flag), before performing `new byte[len][]`. Additionally, tie the energy cost of `ValidateMultiSign`/`BatchValidateSign` to the declared array length rather than solely `data.length`, and reject calls where the declared length is inconsistent with the actual calldata size before allocation.

### Proof of Concept
Conceptually (cannot be executed in this read-only environment):
1. Build calldata for `validatemultisign(address,uint256,bytes32,bytes[])` where the ABI-encoded offset word for the `bytes[]` array points to a length word set to a very large value (e.g., `0x7fffffff`), while the remainder of the calldata buffer is kept minimal.
2. Submit a `TriggerSmartContract` transaction calling the `ValidateMultiSign` precompile address (`0x...a`) with this calldata.
3. `DataWord.parseArray(rawData)` parses only actual `data.length` words, but `extractBytesArray`/`extractSigArray` reads `words[offset].intValueSafe()` as `len` and executes `new byte[len][]`, attempting to allocate an array of ~2^31 references before any `MAX_SIZE` check applies (when `allowTvmSelfdestructRestriction` is not yet active) — causing `OutOfMemoryError` / node hang during transaction execution.

Note: I could not fully trace runtime behavior (e.g., whether an earlier bounds check in `DataWord.parseArray` or index-out-of-bounds exception is thrown before reaching the allocation for all code paths) due to the size limits of the codebase index; a Devin session with full repository access and test execution would be needed to conclusively reproduce the crash and confirm exact conditions under which `allowTvmSelfdestructRestriction` is/is not active on mainnet.

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1045-1049)
```java
    public long getEnergyForData(byte[] data) {
      long cnt = (data.length / WORD_SIZE - 5) / 5;
      // one sign 1500, half of ecrecover
      return cnt * ENGERYPERSIGN;
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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1173-1181)
```java
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
