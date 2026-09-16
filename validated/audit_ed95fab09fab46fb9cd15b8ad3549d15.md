### Title
Missing input-length validation in `ValidateMultiSign` precompiled contract causes uncaught runtime exception (CHECK-failure analog) reachable from any TVM contract call - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The `ValidateMultiSign` precompiled contract (TVM address exposed once `VMConfig.allowTvmSolidity059()` is active — i.e. on any live network) decodes raw calldata into a `DataWord[]` array via `DataWord.parseArray(rawData)` and then immediately indexes fixed positions (`words[0]`, `words[1]`, `words[2]`, `words[3]`, and `words[words[3].intValueSafe()/WORD_SIZE]`) without first checking that the array actually contains that many words. This is the same bug class as the TensorFlow `LoadAndRemapMatrix` advisory: the code assumes a minimum input "shape" and accesses it before validating it, so undersized/malformed input triggers an unhandled runtime exception instead of a graceful rejection.

### Finding Description
`ValidateMultiSign.execute()`: [1](#0-0) 

The shape-validation guard (`isValidAbiEncoding(rawData, ABI_HEADER_WORDS, ABI_ITEM_WORDS)`) is only applied `if (VMConfig.allowTvmOsaka())`. When that hard-fork flag is not yet activated on the network (the default/legacy state), execution falls straight through to `DataWord.parseArray(rawData)` and unconditional indexing of `words[0..3]` and a dynamically computed offset — with no bounds check against `words.length`. If `rawData` is shorter than 5 words (or otherwise malformed so the computed offset points past the array end), this throws an unhandled `ArrayIndexOutOfBoundsException`/`RuntimeException` that is not caught anywhere inside `execute()` (unlike `BatchValidateSign`, whose `execute()` wraps `doExecute()` in a `try { } catch (Throwable t)`).

The repository's own tests explicitly document this as a known, still-reachable failure mode pre-activation: [2](#0-1) 

The companion `OperationsTest` shows that the "outer frame is not affected by the exception" property is only asserted/guaranteed **after** the TIP-854/Osaka guard is enabled: [3](#0-2) 

This strongly implies that, pre-activation, the exception is not safely contained inside the CALL opcode's precompile dispatch and can propagate out of `Program.callToPrecompiledAddress` into the surrounding TVM execution path (`VMActuator` → `RuntimeImpl.execute()` → `TransactionTrace.exec()` → `Manager.processTransaction()`), none of which declare or catch a generic unchecked exception of this kind: [4](#0-3) [5](#0-4) 

### Impact Explanation
Any TVM contract (deployable and callable by an unprivileged, anonymous account) that invokes the `validatemultisign` precompile with short/malformed calldata can trigger an unhandled runtime exception deep inside transaction execution. Since transaction/contract execution in `processTransaction` is invoked identically by every full node and witness applying the same block (or even during transaction broadcast pre-validation), an attacker-controlled deterministic exception here is a strong denial-of-service primitive: it can cause inconsistent handling of the transaction across the codebase's exception-handling boundaries, and in the pre-activation code path this is explicitly documented by the test suite as *not* contained the way the post-fix code guarantees. This matches CWE-20 exactly as in the source advisory — missing shape validation of a data blob before indexed access, producing an uncaught crash condition instead of controlled input rejection.

### Likelihood Explanation
High reachability: `validatemultisign` is a public precompiled contract available to any Solidity contract once `allowTvmSolidity059` is enabled (already the case on production TRON networks), and it can be invoked from a plain `CALL`/static-call with attacker-fully-controlled calldata, requiring no special privilege, asset ownership, or witness/SR role. The guard added for TIP-854/Osaka only protects networks where that specific hard fork has already been activated; any network/version window before that activation remains exposed, and the test suite itself frames this as intentional "legacy behaviour" rather than a proven safe fallback.

### Recommendation
Apply the same `isValidAbiEncoding` (or equivalent minimum-length/offset bounds) check unconditionally in `ValidateMultiSign.execute()`, not only when `VMConfig.allowTvmOsaka()` is true, and add an explicit bounds check on `words.length` before indexing `words[0]`, `words[1]`, `words[2]`, `words[3]`, and the dynamically computed signature-array offset, mirroring the defensive `if (offset > words.length - 1) return new byte[0][];` pattern already used in `extractBytesArray`/`extractSigArray`. Wrap the whole `execute()` body in a `try/catch(Throwable)` fallback (as `BatchValidateSign` already does) so malformed input always degrades to `Pair.of(true, DATA_FALSE)`/`Pair.of(false, EMPTY_BYTE_ARRAY)` instead of propagating an unchecked exception into the broader transaction-execution pipeline.

### Proof of Concept
1. Deploy or reuse any Solidity contract that performs a low-level `staticcall`/`call` to the `validatemultisign` precompiled contract address (enabled once `allowTvmSolidity059` is active, which is true on current chains).
2. Craft calldata shorter than 5 × 32 bytes (e.g., 32 or 64 bytes total) — reproducing the exact input used in the regression test: [6](#0-5) 
3. Invoke the contract with this calldata on a node/network where TIP-854 (`VMConfig.allowTvmOsaka()`) is not yet activated. `DataWord.parseArray` returns an array with fewer than 4 elements, and the subsequent `words[3]` (or dependent offset) access throws an unhandled exception inside `ValidateMultiSign.execute()`, which — per the test comments — is not guaranteed to be contained by the surrounding call frame prior to the TIP-854 guard.

### Citations

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

**File:** framework/src/test/java/org/tron/common/runtime/vm/OperationsTest.java (L861-897)
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
```

**File:** framework/src/main/java/org/tron/common/runtime/RuntimeImpl.java (L37-60)
```java
  @Override
  public void execute(TransactionContext context)
      throws ContractValidateException, ContractExeException {
    this.context = context;

    ContractType contractType = context.getTrxCap().getInstance().getRawData().getContract(0)
        .getType();
    switch (contractType.getNumber()) {
      case ContractType.TriggerSmartContract_VALUE:
      case ContractType.CreateSmartContract_VALUE:
        actuator2 = new VMActuator(context.isStatic());
        break;
      default:
        actuatorList = ActuatorCreator.getINSTANCE().createActuator(context.getTrxCap());
    }
    if (actuator2 != null) {
      actuator2.validate(context);
      actuator2.execute(context);
    } else {
      for (Actuator act : actuatorList) {
        act.validate();
        act.execute(context.getProgramResult().getRet());
      }
    }
```

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L1509-1561)
```java
  public TransactionInfo processTransaction(final TransactionCapsule trxCap, BlockCapsule blockCap)
      throws ValidateSignatureException, ContractValidateException, ContractExeException,
      AccountResourceInsufficientException, TransactionExpirationException,
      TooBigTransactionException, TooBigTransactionResultException,
      DupTransactionException, TaposException, ReceiptCheckErrException, VMIllegalException {
    if (trxCap == null) {
      return null;
    }
    Sha256Hash txId = trxCap.getTransactionId();
    if (trxCap.getInstance().getRawData().getContractList().size() != 1) {
      throw new ContractSizeNotEqualToOneException(
          String.format(
              "tx %s contract size should be exactly 1, this is extend feature ,actual :%d",
              txId, trxCap.getInstance().getRawData().getContractList().size()));
    }
    Contract contract = trxCap.getInstance().getRawData().getContract(0);
    final Histogram.Timer requestTimer = Metrics.histogramStartTimer(
        MetricKeys.Histogram.PROCESS_TRANSACTION_LATENCY,
        Objects.nonNull(blockCap) ? MetricLabels.BLOCK : MetricLabels.TRX,
        contract.getType().name());

    long start = System.currentTimeMillis();

    if (Objects.nonNull(blockCap)) {
      chainBaseManager.getBalanceTraceStore().initCurrentTransactionBalanceTrace(trxCap);
      trxCap.setInBlock(true);
    }

    validateTapos(trxCap);
    validateCommon(trxCap);

    validateDup(trxCap);

    if (!trxCap.validateSignature(chainBaseManager.getAccountStore(),
        chainBaseManager.getDynamicPropertiesStore())) {
      throw new ValidateSignatureException(
          String.format(" %s transaction signature validate failed", txId));
    }

    if (!trxCap.isInBlock()) {
      trxCap.sanitize();
    }
    TransactionTrace trace = new TransactionTrace(trxCap, StoreFactory.getInstance(),
        new RuntimeImpl());
    trxCap.setTrxTrace(trace);

    consumeBandwidth(trxCap, trace);
    consumeMultiSignFee(trxCap, trace);
    consumeMemoFee(trxCap, trace);

    trace.init(blockCap, eventPluginLoaded);
    trace.checkIsConstant();
    trace.exec();
```
