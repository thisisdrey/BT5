### Title
Missing bounds validation on attacker-controlled array offset/length in `ValidateMultiSign` precompiled contract causes uncaught out-of-bounds exception - ([File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java])

### Summary
The `ValidateMultiSign` TVM precompiled contract (invoked via the `validatemultisign(address,uint256,bytes32,bytes[])` precompile, callable by any signed transaction or constant call that reaches the TVM) decodes an attacker-supplied `bytes[]` array from raw calldata using `extractBytesArray`/`extractSigArray`, which trust an offset/length value taken directly from the calldata word array without validating it against the actual size of the decoded `DataWord[]` array before indexing into it. This mirrors the exfat bug class: an index/count value derived from untrusted input is used to access an array without first checking that it is within the array's valid bounds.

### Finding Description
`extractBytesArray` and `extractSigArray` read a "length" word from the caller-supplied `words` array and then loop reading `words[offset + i + 1]` that many times, with only a shallow `offset > words.length - 1` guard that does not account for `len`: [1](#0-0) 

`ValidateMultiSign.execute()` calls these helpers with an offset (`words[3].intValueSafe() / WORD_SIZE`) that is entirely attacker-controlled via the raw calldata, **before** entering the only `try/catch` block in the method (which starts later, at the `account.getPermissionById(...)` call): [2](#0-1) 

If the caller crafts calldata so that the decoded `len` (from `words[offset]`) is larger than the number of words actually present after `offset` in the parsed `DataWord[]` array, `words[offset + i + 1]` throws an uncaught `ArrayIndexOutOfBoundsException` that propagates out of `execute()` — it is **not** caught by `ValidateMultiSign` itself (unlike `BatchValidateSign.execute()`, which wraps its equivalent logic in an outer `catch (Throwable t)`).

This exact behavior is confirmed and explicitly documented in the test suite as "existing (legacy) behaviour": [3](#0-2) 

A later hard fork (TIP-854 / `VMConfig.allowTvmOsaka()`) added an `isValidAbiEncoding` pre-check, but this guard is **only active when the Osaka feature is enabled**; the underlying legacy path with the unguarded array access remains present and reachable whenever that feature is not active: [4](#0-3) 

### Impact Explanation
The `validatemultisign` precompile is reachable by any contract call (including `TriggerConstantContract`/`TriggerSmartContract` requests through Wallet/TronJsonRpcImpl, and any deployed smart contract that calls the precompiled address) with fully attacker-controlled `bytes` calldata. Supplying a short/malformed byte array causes an uncaught `ArrayIndexOutOfBoundsException` to escape the precompile's `execute()` method during TVM opcode dispatch, propagating up through `Program`/`VM` execution. Depending on how far up the call stack this unchecked exception is caught, this can disrupt or crash the execution thread handling the transaction/query, denying that request; because both broadcast transactions and constant-call (query-only) API paths can trigger it, it can be used to repeatedly probe and disrupt normal TVM execution paths on the node — matching the exfat analog of "no validation of an index/count before array access leading to out-of-bounds read."

### Likelihood Explanation
High reachability: the precompile is invoked purely from calldata that any unprivileged account can construct and submit either as a broadcast transaction or an `EstimateEnergy`/`TriggerConstantContract` query, with no special permissions required. The vulnerable code path (pre-Osaka legacy behavior) is confirmed still present and is explicitly acknowledged by the project's own test comments as throwing an uncaught `RuntimeException`.

### Recommendation
Apply the same fix pattern already used for `BatchValidateSign` (defensive `try/catch(Throwable)` wrapping the entire `execute()` body) and/or make the `isValidAbiEncoding`/length-bounds validation unconditional (not gated behind `VMConfig.allowTvmOsaka()`) in `ValidateMultiSign.execute()`, `extractBytesArray`, `extractSigArray`, and `extractBytes32Array`, validating that `offset + i + 1 < words.length` for all `i < len` before indexing, rejecting the call gracefully instead of throwing.

### Proof of Concept
Call the `validatemultisign(address,uint256,bytes32,bytes[])` precompile (address `0x66...09`) with `VMConfig.allowTvmOsaka()` disabled (default/legacy behavior) and calldata shorter than what the fourth header word claims as the `bytes[]` array's element count/offset — e.g., as reproduced in the project's own test: [5](#0-4) 
This throws a `RuntimeException` (`ArrayIndexOutOfBoundsException`) directly from `extractBytesArray`/`extractSigArray`, uncaught by `ValidateMultiSign`.

### Citations

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1051-1081)
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
```

**File:** framework/src/test/java/org/tron/common/runtime/vm/ValidateMultiSignContractTest.java (L244-260)
```java
  // TIP-854: before activation, malformed calldata reaches the legacy decoder.
  // Assert the guard is not taken — this precompile has no outer catch, so a
  // too-short input raises inside the decoder; that is the documented
  // pre-activation failure mode the TIP explicitly preserves.
  @Test
  public void testTip854PreActivationNoOp() {
    VMConfig.initAllowTvmOsaka(0);
    contract.setRepository(RepositoryImpl.createRoot(StoreFactory.getInstance()));
    try {
      Pair<Boolean, byte[]> ret = contract.execute(new byte[(5 + 1) * 32]);
      // If the decoder happened to handle it without raising, we must not have
      // taken the post-activation reject path (false, empty).
      Assert.assertNotSame(ByteUtil.EMPTY_BYTE_ARRAY, ret.getRight());
    } catch (RuntimeException expectedLegacyBehaviour) {
      // Pre-activation: decoder may throw — this is the existing behaviour.
    }
  }
```
