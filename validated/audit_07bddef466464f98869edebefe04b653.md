### Title
Malformed length field in `ValidateMultiSign`/`BatchValidateSign` precompile ABI decoding causes unbounded array allocation / OOM crash - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The TVM precompiled contracts `ValidateMultiSign` (address `0x...a`) and `BatchValidateSign` (address `0x...9`) decode a caller-supplied dynamic `bytes[]`/`address[]` array by reading a single 256-bit "length" word straight out of attacker-controlled call data and using it, unvalidated against the actual size of the data buffer, as the dimension of a freshly allocated Java array. This mirrors the libpng `sCAL` chunk bug (CVE-2010-2249): a length field taken from untrusted input drives a memory allocation without cross-checking it against the amount of data actually available, allowing a memory-consumption/crash denial of service.

### Finding Description
`extractBytesArray`, `extractSigArray`, and `extractBytes32Array` all read the array length directly from the decoded `DataWord` at the caller-supplied offset and immediately allocate an array of that size: [1](#0-0) [2](#0-1) [3](#0-2) 

These helpers are invoked from `ValidateMultiSign.execute` and `BatchValidateSign.doExecute`, both of which are reachable directly from a `TriggerSmartContract`/`CREATE` transaction that executes a `CALL`/`STATICCALL` to the fixed precompile address: [4](#0-3) [5](#0-4) 

The size guard (`sigArraySize > MAX_SIZE`) that would bound this allocation is only applied when `VMConfig.allowTvmSelfdestructRestriction()` is active, and even then it merely reads one more attacker-controlled word before the bounded/unbounded extraction path is chosen — it does not validate that the length is consistent with the physical length of `rawData`/`data`: [6](#0-5) 

Crucially, the energy charged for the call is computed from the *physical* byte length of the call data, not from the length word actually used to size the allocation: [7](#0-6) [8](#0-7) 

This is exactly the CVE-2010-2249 pattern: a length field embedded in attacker-controlled input is trusted for memory allocation instead of being validated against the amount of actual accompanying data, so a small amount of call data (cheap in energy) can encode an enormous length word (e.g., `0xFFFFFFFF...`) and force the node to attempt an allocation of `new byte[huge][]`, `new byte[huge][32]`, etc. Whether this produces `NegativeArraySizeException`/`OutOfMemoryError` depends on how `DataWord.intValueSafe()` clamps out-of-range 256-bit values, but in either case the exception is not caught in `PrecompiledContract.execute` for `ValidateMultiSign` (no surrounding `try/catch` around the extraction code, only around the later account/permission logic) nor in the caller `callToPrecompiledAddress`, which invokes `contract.execute(data)` with no local exception handling: [9](#0-8) 

An uncaught `OutOfMemoryError`/`Error` thrown mid-transaction execution can destabilize or crash the executing node/thread, i.e. the same "memory consumption and application crash" impact described in the CVE.

### Impact Explanation
A single crafted, signed smart-contract call (anyone with TRX for fees can deploy/trigger a contract invoking these precompiles) can force the node to attempt allocating an oversized array, leading to `OutOfMemoryError`/crash of the transaction-processing thread, or at minimum an uncontrolled resource-consumption spike disproportionate to the energy actually charged. This is a node-crash/DoS class impact, consistent with what the rules require (node crash or halt).

### Likelihood Explanation
The precompiles are reachable by any unprivileged account that can broadcast a `TriggerSmartContract` transaction targeting address `0x...9` or `0x...a` with hand-crafted call data (no special permissions, no dependence on being an SR/witness). Building the malicious calldata only requires knowledge of the ABI layout already documented by java-tron's own test suite for these two precompiles. The `allowTvmSelfdestructRestriction`/TIP-854 guard mitigates but does not eliminate the class, since it only reads and range-checks one more attacker-controlled word rather than validating length against the true data size, and it is not universally active pre-activation.

### Recommendation
- In `extractBytesArray`, `extractSigArray`, and `extractBytes32Array`, validate the decoded `len` against a hard maximum (e.g., `MAX_SIZE`) and against the physical remaining length of `data`/`rawData` *before* allocating any array, unconditionally (not gated behind `allowTvmSelfdestructRestriction`).
- Tie the energy cost computation to the actual decoded array length rather than solely to `data.length`, so undercharging is not possible.
- Wrap the length-derived allocation/extraction logic in `ValidateMultiSign.execute` in the same defensive try/catch already used for the account/permission logic, and ensure `Program.callToPrecompiledAddress` cannot propagate uncaught `Error`s out of contract execution.

### Proof of Concept
1. Craft calldata for `validatemultisign(address,uint256,bytes32,bytes[])` (or `batchvalidatesign(bytes32,bytes[],address[])`) where the static head words are well formed but the length word for the dynamic `bytes[]` array (at the offset pointed to by the corresponding head word) is set to a very large 256-bit value (e.g. `0xFFFFFFFF`).
2. Keep the physical call-data buffer short so `getEnergyForData` computes a small/negative `cnt` and thus a low energy charge, while `VMConfig.allowTvmSelfdestructRestriction()` is disabled (pre-TIP-854 chain state), causing `extractBytesArray`/`extractSigArray` to be invoked directly with the unbounded `len`.
3. Broadcast a `TriggerSmartContract` transaction that performs `CALL` to the precompile address with this calldata.
4. Observe the node attempts to allocate `new byte[len][]` sized by the attacker-controlled length word, leading to `OutOfMemoryError`/crash or severe resource exhaustion on the executing node, uncaught by `Program.callToPrecompiledAddress` or `ValidateMultiSign.execute`.

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1044-1049)
```java
    @Override
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

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L1734-1774)
```java
    long requiredEnergy = contract.getEnergyForData(data);
    if (requiredEnergy > msg.getEnergy().longValue()) {
      // Not need to throw an exception, method caller needn't know that
      // regard as consumed the energy
      this.refundEnergy(0, CALL_PRE_COMPILED); //matches cpp logic
      this.stackPushZero();
    } else {
      // Delegate or not. if is delegated, we will use msg sender, otherwise use contract address
      if (msg.getOpCode() == Op.DELEGATECALL) {
        contract.setCallerAddress(getCallerAddress().toTronAddress());
      } else {
        contract.setCallerAddress(getContextAddress());
      }
      // this is the depositImpl, not contractState as above
      contract.setRepository(deposit);
      contract.setResult(this.result);
      contract.setConstantCall(isConstantCall());
      contract.setVmShouldEndInUs(getVmShouldEndInUs());
      Pair<Boolean, byte[]> out = contract.execute(data);

      if (out.getLeft()) { // success
        this.refundEnergy(msg.getEnergy().longValue() - requiredEnergy, CALL_PRE_COMPILED);
        this.stackPushOne();
        returnDataBuffer = out.getRight();
        deposit.commit();
      } else {
        // spend all energy on failure, push zero and revert state changes
        this.refundEnergy(0, CALL_PRE_COMPILED);
        this.stackPushZero();
        if (Objects.nonNull(this.result.getException())) {
          throw result.getException();
        }
      }

      if (VMConfig.allowTvmSelfdestructRestriction()) {
        this.memorySave(msg.getOutDataOffs().intValueSafe(), msg.getOutDataSize().intValueSafe(), out.getRight());
      } else {
        this.memorySave(msg.getOutDataOffs().intValue(), out.getRight());
      }
    }
  }
```
