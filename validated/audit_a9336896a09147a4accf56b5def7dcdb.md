## Finding

### Title
Unbounded attacker-controlled array-length in `BatchValidateSign`/`ValidateMultiSign` precompiles causes uncontrolled memory allocation (node OOM / DoS) - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The `ValidateMultiSign` (address `0x...0a`) and `BatchValidateSign` (address `0x...09`) TVM precompiled contracts decode ABI-style calldata by reading a "length" word directly from attacker-supplied bytes and using it, unvalidated, to size a Java array via `new byte[len][]`. Any account can reach these precompiles by simply calling them from a smart contract, without needing any special permission. When the length word decodes to a large value, the allocation attempt (`new byte[Integer.MAX_VALUE][]`) triggers immediate massive memory demand / `OutOfMemoryError` on every full node that executes or re-validates the transaction.

### Finding Description
`extractBytesArray`, `extractBytes32Array`, and `extractSigArray` all read an untrusted length directly from the calldata and allocate arrays before any bound is enforced: [1](#0-0) 

`DataWord.intValueSafe()` returns `Integer.MAX_VALUE` whenever the word occupies more than 4 bytes (i.e. it never throws), so an attacker only needs to place a value ≥ 2^32 in the relevant word to force `len = Integer.MAX_VALUE`: [2](#0-1) 

In `BatchValidateSign.doExecute`, the call to `extractBytes32Array(words, words[2].intValueSafe() / WORD_SIZE)` for the address array is only preceded by a `MAX_SIZE` bounds check when `VMConfig.allowTvmSelfdestructRestriction()` is enabled; the `extractBytesArray`/`extractSigArray` selection for signatures has the same conditional guard: [3](#0-2) 

The equivalent branch exists in `ValidateMultiSign.execute`: [4](#0-3) 

When `allowTvmSelfdestructRestriction()` is **not** active (e.g., a network/sidechain/private chain that has not yet activated this particular TIP, or any deployment lagging that hard-fork flag), `extractBytesArray`/`extractSigArray`/`extractBytes32Array` are invoked with **no prior `MAX_SIZE` check at all**, and the very first statement in each is `new byte[len][]` with `len` fully attacker-controlled and unbounded by the actual calldata size. This mirrors the CVE-2026-33754 bug class exactly: a length field is trusted and used to size an allocation before any validation of that length against the actual payload.

### Impact Explanation
A single crafted transaction that calls the `BatchValidateSign` or `ValidateMultiSign` precompile with a malformed "array length" word causes the executing JVM to attempt an allocation on the order of `Integer.MAX_VALUE` array-reference slots (multi-gigabyte). This is deterministic TVM execution, so **every full node** that processes or re-validates the transaction/block hits the same allocation and can suffer `OutOfMemoryError`/thrashing, i.e., unauthenticated remote denial of service of the node process — matching "node crash or halt" in the validation criteria.

### Likelihood Explanation
Both precompiles are reachable by any account issuing a normal contract call (no special permission, no TVM opcode restrictions) as long as `VMConfig.allowTvmSolidity059()` is enabled (a long-activated TIP gating precompile addresses `0x9`/`0xa`). The lack of a length bound is fully controlled by whether `VMConfig.allowTvmSelfdestructRestriction()` — a separate, later TIP — is active on the target network. On any java-tron network/deployment where that specific flag has not yet been switched on, exploitation requires only crafting calldata and does not need the signatures/addresses arrays to actually be large.

### Recommendation
Move the `MAX_SIZE`/length-bound validation in `ValidateMultiSign.execute` and `BatchValidateSign.doExecute` out of the `VMConfig.allowTvmSelfdestructRestriction()` conditional so it is unconditionally enforced before calling `extractBytesArray`, `extractSigArray`, or `extractBytes32Array`. Additionally, harden the low-level extraction helpers themselves (`extractBytesArray`, `extractBytes32Array`, `extractSigArray`) to reject any `len` that is inconsistent with the actual `words.length`/`data.length` before performing `new byte[len][]`.

### Proof of Concept
1. Deploy/call a contract that performs a `CALL` (or `STATICCALL`) to precompile address `0x0000000000000000000000000000000000000000000000000000000000000009` (`BatchValidateSign`) on a network where `allowTvmSelfdestructRestriction` has not been activated.
2. Construct calldata following the ABI layout `(bytes32 hash, bytes[] signatures, bytes32[] addresses)` where the header word encoding the offset to the `addresses` array points to a word whose value is set to `0x0100000000` (5 bytes, > 4-byte range) instead of a plausible count.
3. `words[2].intValueSafe()` resolves the offset, and `extractBytes32Array` reads `len = words[offset].intValueSafe()` = `Integer.MAX_VALUE`, then executes `new byte[Integer.MAX_VALUE][]`, forcing a multi-gigabyte allocation attempt on the node executing the call.
4. The same crafted transaction, once broadcast, is replayed by every full node validating the block, producing a fleet-wide `OutOfMemoryError`/DoS condition.

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1052-1078)
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

      if (signatures.length == 0 || signatures.length > MAX_SIZE) {
        return Pair.of(true, DATA_FALSE);
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
