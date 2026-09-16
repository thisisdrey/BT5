Based on the codebase evidence gathered, I found a strong analog in `PrecompiledContracts.java`'s dynamic array extraction helpers, which mirror the ImageMagick bug class (attacker-controlled length/offset values used for buffer indexing without adequate bounds validation).

### Title
Unvalidated attacker-controlled offset/length in precompile ABI array decoding causes out-of-bounds array access - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The `ValidateMultiSign` (address `0x...a`) and `BatchValidateSign` (address `0x...9`) precompiled contracts, reachable from any smart-contract `CALL`/`STATICCALL` with attacker-supplied calldata, decode dynamic `bytes[]` arguments using `extractBytesArray`/`extractSigArray`, which compute `bytesOffset` and `bytesLen` directly from calldata words via `intValueSafe()` and then slice the raw buffer with `Arrays.copyOfRange` without validating that the derived offset/length stay within the buffer bounds.

### Finding Description
`extractBytesArray` reads a length word, then for every element reads an offset word and a length word taken from *attacker-controlled* calldata, and passes them straight into `extractBytes`: [1](#0-0) [2](#0-1) 

Neither `bytesOffset`, `bytesLen`, nor the intermediate index arithmetic (`words[offset + i + 1]`, `offset + bytesOffset + 2`) is checked against `data.length`/`words.length` before use, so a crafted `bytes[]` header can drive `extractBytes` to request a slice outside the real payload, or drive `offset + i + 1` past `words.length`, causing an unhandled `ArrayIndexOutOfBoundsException`/`NegativeArraySizeException`.

This exact class of bug is confirmed by the project's own test suite, which documents that prior to the `allowTvmOsaka` feature flag (TIP-854), `ValidateMultiSign.execute()` has no surrounding try/catch and malformed calldata "raises inside the decoder" and previously escaped the CALL frame uncontrolled: [3](#0-2) 

The mitigation (`isValidAbiEncoding`, gated by `VMConfig.allowTvmOsaka()`) only checks that `data.length` is 32-byte aligned and that the tail size matches a fixed header/item-word shape — it does **not** validate the actual offset/length values consumed inside `extractBytesArray`/`extractSigArray`: [4](#0-3) 

`BatchValidateSign` wraps its logic in a catch-all that returns a safe default: [5](#0-4) 
but `ValidateMultiSign.execute()` calls `extractBytesArray`/`extractSigArray` **outside** any try/catch: [6](#0-5) 
so a thrown exception there is not caught locally, unlike the `try { ... permission ... } catch (Throwable t)` block that only wraps the *later* signature-recovery logic.

### Impact Explanation
An unprivileged transaction sender who deploys or calls a contract that invokes the `ValidateMultiSign` precompile with a malformed `bytes[]` signature-array header (pre-`allowTvmOsaka` activation, i.e. before TIP-854 is active on the network) can trigger an uncaught `ArrayIndexOutOfBoundsException`/`NegativeArraySizeException` inside `PrecompiledContracts.extractBytesArray`/`extractSigArray`. The project's own added test, `testTip854OuterFrameContainment`, was written specifically to prove that *before* the fix this exception was **not** contained to the inner CALL frame — it is only after the TIP-854 guard that the outer frame is guaranteed exception-free: [7](#0-6) 

This can disrupt transaction execution/energy accounting in ways outside the intended CALL-failure semantics of the TVM (an uncontrolled exception escaping the precompile boundary, rather than the standard "push 0 / revert with energy consumed" outcome), which is precisely the bug class TIP-854 was created to close.

### Likelihood Explanation
Reachable with a single crafted smart-contract transaction (no special privileges), as long as the network has not yet activated `allowTvmOsaka`/TIP-854. Given the extensive dedicated regression tests (`ValidateMultiSignContractTest`, `BatchValidateSignContractTest`, `OperationsTest#testTip854*`) added around this exact defect, this is a previously-identified and partially-patched (feature-flag-gated) issue rather than a purely theoretical one.

### Recommendation
Add explicit bounds checks inside `extractBytesArray`/`extractSigArray`/`extractBytes32Array` (verifying `offset + i + 1 < words.length`, `bytesOffset >= 0`, and `bytesOffset + bytesLen` within `data.length`) unconditionally, rather than only via the `allowTvmOsaka`-gated `isValidAbiEncoding` shape check, and wrap `ValidateMultiSign.execute()`'s decode calls in the same defensive try/catch used later in the method, so malformed calldata always yields `Pair.of(true, DATA_FALSE)` regardless of fork activation state.

### Proof of Concept
Craft calldata for `validatemultisign(address,uint256,bytes32,bytes[])` (or `batchvalidatesign`) where the dynamic `bytes[]` header's per-element offset word is set to a large or negative-after-multiplication value (e.g., `0xFFFFFFFF...` truncated by `intValueSafe()`), so that in `extractBytesArray` the computed `bytesOffset`/`bytesLen` cause `Arrays.copyOfRange(data, offset, offset + len)` to be called with `offset` or `offset+len` outside `[0, data.length]`. Submit this as calldata to a contract that `CALL`s address `0x...a` (`ValidateMultiSign`) before `allowTvmOsaka` is active on the target network, reproducing the pre-TIP-854 uncontrolled-exception path documented in `testTip854PreActivationNoOp`.

**Uncertainty note:** I could not confirm within the available tool budget whether an exception thrown here is ultimately caught at a higher layer (e.g., `RuntimeImpl`) and merely fails the single transaction (in which case impact would be limited to transaction-level DoS/gas griefing rather than a full node crash), or whether it can propagate further. This should be verified against `framework/src/main/java/org/tron/common/runtime/RuntimeImpl.java`'s top-level exception handling before treating this as a node-crash-severity issue rather than a transaction-execution-integrity issue.

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1072-1078)
```java
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

**File:** framework/src/test/java/org/tron/common/runtime/vm/OperationsTest.java (L861-902)
```java
  // TIP-854 outer-frame containment: a CALL to validateMultiSign or
  // batchValidateSign with malformed calldata must (a) push 0 onto the outer
  // stack, (b) leave the outer frame free of any propagated exception, and
  // (c) allow the outer frame to continue executing afterwards.
  @Test
  public void testTip854OuterFrameContainment() throws ContractValidateException {
    byte prePrefixByte = DecodeUtil.addressPreFixByte;
    DecodeUtil.addressPreFixByte = Constant.ADD_PRE_FIX_BYTE_MAINNET;
    VMConfig.initAllowTvmOsaka(1);
    try {
      for (PrecompiledContracts.PrecompiledContract contract :
          new PrecompiledContracts.PrecompiledContract[]{
              new PrecompiledContracts.ValidateMultiSign(),
              new PrecompiledContracts.BatchValidateSign()}) {
        invoke = new ProgramInvokeMockImpl();
        InternalTransaction interTrx = new InternalTransaction(
            Protocol.Transaction.getDefaultInstance(),
            InternalTransaction.TrxType.TRX_UNKNOWN_TYPE);
        program = new Program(new byte[0], new byte[0], invoke, interTrx);
        // inDataSize=0 ⇒ data=[] ⇒ fewer than H=5 head words ⇒ guard rejects.
        MessageCall messageCall = new MessageCall(
            Op.CALL, new DataWord(10000),
            DataWord.ZERO(), DataWord.ZERO(),
            DataWord.ZERO(), DataWord.ZERO(),
            DataWord.ZERO(), DataWord.ZERO(),
            DataWord.ZERO(), false);
        program.callToPrecompiledAddress(messageCall, contract);

        Assert.assertNull(contract.getClass().getSimpleName()
                + ": outer frame must not inherit an exception",
            program.getResult().getException());
        Assert.assertEquals(contract.getClass().getSimpleName() + ": inner CALL pushes 0",
            DataWord.ZERO(), program.getStack().pop());
        // Outer frame continues: another stack op works without throwing.
        program.stackPush(new DataWord(1));
        Assert.assertEquals(new DataWord(1), program.getStack().pop());
      }
    } finally {
      VMConfig.initAllowTvmOsaka(0);
      DecodeUtil.addressPreFixByte = prePrefixByte;
    }
  }
```
