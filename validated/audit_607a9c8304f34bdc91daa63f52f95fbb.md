### Title
Insufficient input validation in ValidateMultiSign precompile allows attacker-controlled array sizes/offsets to throw uncaught exceptions/OOM out of TVM execution - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
The `ValidateMultiSign` precompiled contract (address `0x...a`, reachable from any Solidity contract via `address(0xa).staticcall/call(...)`) decodes attacker-supplied calldata into offset/length fields and uses them directly to size and index Java arrays, without validating them against the actual `data` length before allocation, unlike its sibling `BatchValidateSign` which wraps the equivalent logic in a blanket `catch (Throwable t)`.

### Finding Description
`ValidateMultiSign.execute()` reads calldata-controlled words and immediately uses them to compute array sizes and offsets: [1](#0-0) 

`extractBytesArray`/`extractSigArray`/`extractBytes32Array` allocate `new byte[len][]` where `len` comes straight from `words[offset].intValueSafe()`, and compute nested offsets (`bytesOffset`, `bytesLen`) from further attacker-controlled words, with no check that these values fit inside the actual `data`/`words` array bounds: [2](#0-1) 

The only bound check (`sigArraySize > MAX_SIZE`) is gated behind `VMConfig.allowTvmSelfdestructRestriction()`, and even that check itself indexes `words[...]` with an attacker-controlled offset before any bounds validation, so it can itself throw. Crucially, unlike `BatchValidateSign.execute()` — which wraps its entire body in `try { return doExecute(data); } catch (Throwable t) { ... }` — `ValidateMultiSign.execute()` has no such catch-all around the decode/extraction path; the only `try/catch` in the method wraps the later account-permission logic, not the array construction: [3](#0-2) 

The recently-added TIP-854 mitigation (`isValidAbiEncoding` check, gated by `VMConfig.allowTvmOsaka()`) rejects malformed ABI shapes cleanly before this decoding happens — but only once the Osaka hard fork flag is activated. The project's own tests document that pre-activation, malformed calldata reaching these decoders "may throw — this is the existing behaviour": [4](#0-3) 

The `OperationsTest.testTip854OuterFrameContainment` test explicitly verifies that only with `VMConfig.initAllowTvmOsaka(1)` does the outer TVM frame stay free of propagated exceptions for both precompiles, implying that without that config flag active the exception previously escaped into the interpreter's outer call frame: [5](#0-4) 

### Impact Explanation
Because `intValueSafe()`-derived lengths/offsets are used directly for array allocation and indexing without bounds checks, and `ValidateMultiSign` has no wrapping `catch(Throwable)`, an attacker's contract calldata can cause:
- `ArrayIndexOutOfBoundsException`/`NegativeArraySizeException` when offsets exceed the actual `words`/`data` array size, and
- potential `OutOfMemoryError` when `len` is coerced to a very large positive value for `new byte[len][]`,
to escape `ValidateMultiSign.execute()` uncaught (until the Osaka activation flag is turned on). If this propagates past the TVM interpreter's per-call exception handling into block/transaction processing, it can crash or destabilize the node processing the block (memory exhaustion / uncontrolled exception matching the OpenRGB CVE's "insufficient input checks → memory exhaustion / out-of-bounds" bug class), constituting a node crash/DoS reachable from a single crafted contract call.

### Likelihood Explanation
Any account can deploy a trivial contract that calls the `ValidateMultiSign` precompile (`0x000...a`) with crafted calldata containing out-of-range offset/length words; no special privileges are required, and `VMConfig.allowTvmOsaka()`/TIP-854 protection is only effective once that specific hard fork flag is activated network-wide, so any deployment window prior to Osaka activation (or any network that has not yet activated it) is exposed.

### Recommendation
Wrap `ValidateMultiSign.execute()`'s decode/extract path in the same defensive `try { ... } catch (Throwable t) { return Pair.of(true, DATA_FALSE); }` pattern already used in `BatchValidateSign.execute()`, and/or make the `isValidAbiEncoding` bounds check unconditional (not gated behind `VMConfig.allowTvmOsaka()`) so malformed ABI shapes are rejected regardless of hard-fork activation state, independent of `extractBytesArray`/`extractSigArray`/`extractBytes32Array` internal bounds validation improvements.

### Proof of Concept
Deploy a contract that performs a low-level call to `0x000000000000000000000000000000000000000a` (`ValidateMultiSign`) with calldata whose word at the signatures-array offset slot (`words[3]`) points far beyond the actual data length, or whose derived `len`/`bytesOffset`/`bytesLen` values are crafted (e.g. via `0xFFFFFFFF`-style words) to be huge or negative — mirroring the exact shapes exercised in `BatchValidateSignContractTest.testTip854RejectsMalformedCalldata` and `ValidateMultiSignContractTest.testTip854PreActivationNoOp`, but submitted against the live precompile while `VMConfig.allowTvmOsaka()` is not yet active. Absent the outer catch present in `BatchValidateSign`, the resulting `ArrayIndexOutOfBoundsException`/`NegativeArraySizeException`/`OutOfMemoryError` propagates out of `ValidateMultiSign.execute()`.

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1123-1160)
```java
  public static class BatchValidateSign extends PrecompiledContract {

    private static final ExecutorService workers;
    private static final String workersName = "validate-sign-contract";
    private static final int ENGERYPERSIGN = 1500;
    private static final int MAX_SIZE = 16;
    private static final int ABI_HEADER_WORDS = 5;
    private static final int ABI_ITEM_WORDS = 6;

    static {
      workers = ExecutorServiceManager.newFixedThreadPool(workersName,
          Runtime.getRuntime().availableProcessors() / 2 + 1);
    }

    @Override
    public long getEnergyForData(byte[] data) {
      long cnt = (data.length / WORD_SIZE - 5) / 6;
      // one sign 1500, half of ecrecover
      return cnt * ENGERYPERSIGN;
    }

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

    private Pair<Boolean, byte[]> doExecute(byte[] data)
        throws InterruptedException, ExecutionException {
      if (VMConfig.allowTvmOsaka()
          && !isValidAbiEncoding(data, ABI_HEADER_WORDS, ABI_ITEM_WORDS)) {
        return Pair.of(false, EMPTY_BYTE_ARRAY);
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

**File:** framework/src/test/java/org/tron/common/runtime/vm/OperationsTest.java (L861-893)
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
```
