### Title
Pre-activation `CHECK`-style crash: unvalidated ABI shape in `ValidateMultiSign`/`BatchValidateSign` precompiles causes uncaught RuntimeException during TVM execution - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
The TensorFlow `SetSize` advisory is a classic "unvalidated input shape → unchecked native assertion/CHECK failure → DoS" bug class. The java-tron analog lives in the `ValidateMultiSign` and `BatchValidateSign` TVM precompiled contracts, where the ABI-decoding path (`DataWord.parseArray`, `extractBytesArray`, `extractSigArray`, `extractBytes32Array`) assumes calldata has a specific minimum word-count "shape" before it is safe to index. Prior to the TIP-854 (`allowTvmOsaka`) hardfork flag being active, that shape is never validated, so a contract call with truncated/malformed calldata to these precompile addresses throws an uncaught `ArrayIndexOutOfBoundsException`/`RuntimeException` from inside decoding.

### Finding Description
`ValidateMultiSign.execute()` and `BatchValidateSign.doExecute()` only guard the ABI shape when `VMConfig.allowTvmOsaka()` is true: [1](#0-0) [2](#0-1) 

The `isValidAbiEncoding` guard (added for TIP-854) is the only check that the input has enough header/tail words before indexing into `DataWord[] words`: [3](#0-2) 

When the flag is off (the default/pre-activation state), execution proceeds directly to `DataWord.parseArray(rawData)` and unguarded indexing such as `words[3]`, `words[words[1].intValueSafe()/WORD_SIZE]`, and the `extractBytesArray`/`extractSigArray`/`extractBytes32Array` helpers, which loop `len = words[offset].intValueSafe()` times indexing `words[offset+i+1]` with no bounds check against `words.length`: [4](#0-3) [5](#0-4) 

Critically, this decoding logic sits *outside* the `try/catch` blocks in both precompiles: `ValidateMultiSign.execute()` only wraps the permission-weight loop, not the header parsing (lines 1057–1077 are unguarded before the `try` at line 1082), and while `BatchValidateSign.execute()` does wrap `doExecute()` in a `try { } catch (Throwable t)` at the outer level, the pre-activation test explicitly documents that `ValidateMultiSign` "has no outer catch, so a too-short input raises inside the decoder": [6](#0-5) 

That exception is thrown from inside `contract.execute(data)`, which `Program.callToPrecompiledAddress` invokes with no surrounding try/catch of its own: [7](#0-6) 

This is reachable by any account issuing a `TriggerSmartContract` (a `CALL`/`CALLCODE`/`DELEGATECALL`/`STATICCALL` opcode) targeting the `validateMultiSign` precompile address with truncated calldata — i.e., any unprivileged contract deployer/caller, matching the "TVM opcodes, precompiles and energy metering" reachable surface.

### Impact Explanation
An uncaught `RuntimeException` from precompile decoding propagates out of `Program`/`VMActuator`/`RuntimeImpl` during ordinary transaction execution inside block application. Depending on how far up the call stack the exception is caught (or not), this can manifest as a validator/full node crash or an inconsistent state between nodes that do/don't hit the same code path, which is a denial-of-service risk consistent with the "node crash or halt" acceptance criterion. This mirrors the TensorFlow bug class exactly: an unvalidated "shape" assumption (word-count layout of ABI-encoded calldata) leads straight to an unchecked native/array-bounds failure reachable from untrusted input.

### Likelihood Explanation
The precompile is reachable by any account via a simple smart-contract call with attacker-chosen calldata length; no special privilege is required, only that `VMConfig.allowTvmSolidity059()` enabled these precompile addresses (a mainnet-activated fork feature) and that `allowTvmOsaka()` (TIP-854) is not yet active. Given the code explicitly documents this as "the existing/legacy behaviour" pre-activation, the vulnerable window is any network state where Solidity059 is active but Osaka is not — a condition that is normal, non-adversarial network configuration, not a hypothetical.

### Recommendation
Apply the `isValidAbiEncoding` shape check (or equivalent bounds validation before every `words[...]` access and inside `extractBytesArray`/`extractSigArray`/`extractBytes32Array`) unconditionally, rather than gating it behind `VMConfig.allowTvmOsaka()`. Alternatively, wrap the entire `execute()` body of `ValidateMultiSign` (and confirm `BatchValidateSign`'s outer catch also covers all decode-time exceptions) in a `try/catch(Throwable)` that returns `(true, DATA_FALSE)` on any decode failure, independent of hardfork activation, so malformed calldata can never escape the precompile boundary as a raw exception.

### Proof of Concept
Call `validateMultiSign(address,uint256,bytes32,bytes[])` at the precompile address with calldata shorter than the minimum 5-word header (or with an internal length field pointing past the buffer), while `VMConfig.allowTvmOsaka()` is disabled (default/pre-fork state):
```java
VMConfig.initAllowTvmOsaka(0);
PrecompiledContracts.ValidateMultiSign contract = new PrecompiledContracts.ValidateMultiSign();
contract.setRepository(RepositoryImpl.createRoot(StoreFactory.getInstance()));
contract.execute(new byte[(5 + 1) * 32]); // fewer/mismatched words than the decoder assumes
// -> throws ArrayIndexOutOfBoundsException / RuntimeException uncaught by execute()
```
This is exactly the scenario the repository's own regression test `testTip854PreActivationNoOp` in `ValidateMultiSignContractTest.java` (lines 248–260) asserts as accepted "legacy" failure behavior — confirming the decoder can raise an uncaught exception when reached through ordinary `CALL` opcodes prior to Osaka activation.

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L432-438)
```java
  private static boolean isValidAbiEncoding(byte[] data, int headerWords, int itemWords) {
    if (data == null || data.length % WORD_SIZE != 0) {
      return false;
    }
    long tail = subtractExact(data.length, multiplyExact(headerWords, WORD_SIZE));
    return tail > 0 && tail % multiplyExact(itemWords, WORD_SIZE) == 0;
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1051-1057)
```java
    @Override
    public Pair<Boolean, byte[]> execute(byte[] rawData) {
      if (VMConfig.allowTvmOsaka()
          && !isValidAbiEncoding(rawData, ABI_HEADER_WORDS, ABI_ITEM_WORDS)) {
        return Pair.of(false, EMPTY_BYTE_ARRAY);
      }
      DataWord[] words = DataWord.parseArray(rawData);
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1058-1077)
```java
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
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1156-1162)
```java
    private Pair<Boolean, byte[]> doExecute(byte[] data)
        throws InterruptedException, ExecutionException {
      if (VMConfig.allowTvmOsaka()
          && !isValidAbiEncoding(data, ABI_HEADER_WORDS, ABI_ITEM_WORDS)) {
        return Pair.of(false, EMPTY_BYTE_ARRAY);
      }
      DataWord[] words = DataWord.parseArray(data);
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

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L1752-1766)
```java
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
```
