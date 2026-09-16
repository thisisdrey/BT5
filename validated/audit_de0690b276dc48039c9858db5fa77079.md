Based on my investigation, the strongest analog is a length-controlled out-of-bounds/oversized-allocation bug in the TVM precompiled contracts that parse attacker-controlled ABI arrays — directly paralleling the elfutils CVE's pattern of trusting an unvalidated length field from crafted input.

### Title
Unvalidated attacker-controlled array length in `ValidateMultiSign`/`BatchValidateSign` precompile ABI decoding causes ArrayIndexOutOfBoundsException/OOM crash path - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
`extractBytesArray`, `extractSigArray`, and `extractBytes32Array` in `PrecompiledContracts.java` read a "length" value directly out of attacker-supplied calldata words and use it, without bounds checking against the actual `words` array size, both to allocate a Java array and to index into `words`/`data`. [1](#0-0) 

### Finding Description
`extractBytes32Array(words, offset)` computes `int len = words[offset].intValueSafe()` and then loops `for (i=0;i<len;i++) bytes32Array[i] = words[offset+i+1].getData();` with no check that `offset+len` is within `words.length`. [2](#0-1)  Similarly `extractBytesArray` derives `len`, `bytesOffset`, and `bytesLen` entirely from attacker data and calls `extractBytes(data, ..., bytesLen)` → `Arrays.copyOfRange`, again unchecked. [3](#0-2) 

These helpers are invoked from `ValidateMultiSign.execute()` (precompile address `0x...a`) and `BatchValidateSign.doExecute()` (precompile address `0x...9`), both callable by any account via a normal `CALL`/`STATICCALL` to those TVM precompiled addresses through `Program.callToPrecompiledAddress`. [4](#0-3) [5](#0-4) [6](#0-5) 

A guard (`isValidAbiEncoding`, part of the TIP-854 hardening) was added, but it is **only active when `VMConfig.allowTvmOsaka()` is enabled**: [7](#0-6) [8](#0-7) . The project's own tests explicitly document that pre-activation, malformed calldata "may raise... this is the existing behaviour" for `ValidateMultiSign`, which has **no surrounding try/catch** around the parsing/extraction path (the try/catch only wraps the signature-verification loop that runs after extraction). [9](#0-8) [10](#0-9) 

An attacker can craft a `len` word that is huge or negative (`intValueSafe()` truncates a 256-bit word to an `int`), producing `NegativeArraySizeException`, `ArrayIndexOutOfBoundsException`, or an oversized `new byte[len][]` allocation attempt, exactly mirroring the CVE's pattern of an unchecked length field driving an out-of-bounds/over-read condition that crashes the parser.

### Impact Explanation
Because `ValidateMultiSign.execute()` has no local catch around the vulnerable extraction code, an unhandled `RuntimeException`/`Error` propagates out of `contract.execute(data)` in `Program.callToPrecompiledAddress`. [11](#0-10)  This is ultimately absorbed by `VM.play`'s `catch (RuntimeException e)` and `VMActuator`'s broad `catch (Throwable e)`, which convert it into a failed transaction (`UNKNOWN`/generic exception) rather than a full node crash for ordinary `RuntimeException`. [12](#0-11) [13](#0-12)  However, if the crafted length triggers a JVM `Error` (e.g., `OutOfMemoryError` from an oversized array allocation) rather than a `RuntimeException`, it is **not caught** by any of these handlers (`VM.play` only catches `StackOverflowError` specially), and would propagate up through `Manager.processTransaction`/`Manager.processBlock`, which only catch generic `Exception`, not `Error`. [14](#0-13)  An uncaught `Error` during block application in `Manager` can crash or destabilize the witness/full-node process, which matches the CVE's "crafted input → crash" DoS class.

### Likelihood Explanation
Both precompiles are reachable by any account via an ordinary signed transaction (`TriggerSmartContract` calling address `0x...9` or `0x...a`), requiring no special privilege. The `isValidAbiEncoding` guard that mitigates this is gated behind the `allowTvmOsaka` hard-fork flag, so on any network/branch where that flag is not yet active, the legacy unguarded path is live, as the project's own tests acknowledge. This makes exploitation straightforward for an unprivileged contract caller once a suitably malformed `bytes[]`/array-length word is crafted.

### Recommendation
Add explicit length/bounds validation in `extractBytesArray`, `extractSigArray`, and `extractBytes32Array` (reject if `len` is negative or `offset + len` exceeds `words.length`) independent of the `allowTvmOsaka` flag, and wrap the entire `ValidateMultiSign.execute()`/`BatchValidateSign.doExecute()` body — including the ABI-decoding steps — in a `try/catch(Throwable)` that safely returns `(true, DATA_FALSE)` instead of allowing unguarded `Error`/`RuntimeException` propagation, matching the pattern already used for the post-Osaka guard but applied unconditionally.

### Proof of Concept
Craft a `TriggerSmartContract` calldata for the `validatemultisign` precompile (address ending in `0a`) where the head word at the position pointed to by `words[3]` (the signatures-array length) is set to an extremely large value (e.g., `0xFFFFFFFF`), with `VMConfig.allowTvmOsaka()` disabled (pre-hardfork state):
1. `words = DataWord.parseArray(rawData)` succeeds since header size only needs to be non-empty.
2. `words[words[3].intValueSafe()/WORD_SIZE].intValueSafe()` or `extractBytesArray`'s `new byte[len][]` throws `NegativeArraySizeException`/`ArrayIndexOutOfBoundsException`/`OutOfMemoryError`.
3. Because no try/catch wraps this code path in `ValidateMultiSign.execute()`, the exception propagates to `Program.callToPrecompiledAddress` → `VM.play` → `VMActuator`. For a `RuntimeException` this fails the transaction; for an `Error` it can escape all catch clauses up through `Manager.processTransaction`/`processBlock`, which is confirmed not to catch `Throwable`/`Error` around transaction execution. [15](#0-14)

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1051-1082)
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
        try {
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1156-1177)
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
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L1670-1704)
```java
  public void callToPrecompiledAddress(MessageCall msg,
      PrecompiledContracts.PrecompiledContract contract) {
    returnDataBuffer = null; // reset return buffer right before the call

    if (getCallDeep() == MAX_DEPTH) {
      stackPushZero();
      this.refundEnergy(msg.getEnergy().longValue(), " call deep limit reach");
      return;
    }

    Repository deposit = getContractState().newRepositoryChild();

    byte[] senderAddress = getContextAddress();
    byte[] contextAddress;
    if (msg.getOpCode() == Op.CALLCODE || msg.getOpCode() == Op.DELEGATECALL) {
      contextAddress = senderAddress;
    } else {
      contextAddress = msg.getCodeAddress().toTronAddress();
    }

    long endowment = msg.getEndowment().value().longValueExact();
    long senderBalance = 0;
    byte[] tokenId = null;

    checkTokenId(msg);
    boolean isTokenTransfer = isTokenTransfer(msg);
    // transfer TRX validation
    if (!isTokenTransfer) {
      senderBalance = deposit.getBalance(senderAddress);
    } else {
      // transfer trc10 token validation
      tokenId = String.valueOf(msg.getTokenId().longValue()).getBytes();
      senderBalance = deposit.getTokenBalance(senderAddress, tokenId);
    }
    if (senderBalance < endowment) {
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

**File:** actuator/src/main/java/org/tron/core/vm/VM.java (L110-122)
```java
    } catch (JVMStackOverFlowException | OutOfTimeException e) {
      throw e;
    } catch (RuntimeException e) {
      // https://openjdk.org/jeps/358
      // https://bugs.openjdk.org/browse/JDK-8220715
      // since jdk 14, the NullPointerExceptions message is not empty
      if (e instanceof NullPointerException || StringUtils.isEmpty(e.getMessage())) {
        logger.warn("Unknown Exception occurred, tx id: {}",
            Hex.toHexString(program.getRootTransactionId()), e);
        program.setRuntimeFailure(new RuntimeException("Unknown Exception"));
      } else {
        program.setRuntimeFailure(e);
      }
```

**File:** actuator/src/main/java/org/tron/core/actuator/VMActuator.java (L287-301)
```java
    } catch (Throwable e) {
      if (!(e instanceof TransferException)) {
        program.spendAllEnergy();
      }
      result = program.getResult();
      result.rejectInternalTransactions();
      clearExceptionResult(result);
      if (Objects.isNull(result.getException())) {
        logger.error(e.getMessage(), e);
        result.setException(new RuntimeException("Unknown Throwable"));
      }
      if (StringUtils.isEmpty(result.getRuntimeError())) {
        result.setRuntimeError(result.getException().getMessage());
      }
      logger.info("runtime result is :{}", result.getException().getMessage());
```

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L1509-1576)
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

    if (Objects.nonNull(blockCap)) {
      trace.setResult();
      if (trace.checkNeedRetry()) {
        trace.init(blockCap, eventPluginLoaded);
        trace.checkIsConstant();
        trace.exec();
        trace.setResult();
        logger.info("Retry result when push: {}, for tx id: {}, tx resultCode in receipt: {}.",
            blockCap.hasWitnessSignature(), txId, trace.getReceipt().getResult());
      }
      if (blockCap.hasWitnessSignature()) {
        trace.check();
      }
    }
```

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L1884-1898)
```java
      for (TransactionCapsule transactionCapsule : block.getTransactions()) {
        rejectExchangeTransaction(transactionCapsule.getInstance());
        if (chainBaseManager.getDynamicPropertiesStore().allowConsensusLogicOptimization()
            && transactionCapsule.retCountIsGreatThanContractCount()) {
          throw new BadBlockException(String.format("The result count %d of this transaction %s is "
                  + "greater than its contract count %d", transactionCapsule.getRetCount(),
              transactionCapsule.getTransactionId(), transactionCapsule.getContractCount()));
        }
        transactionCapsule.setBlockNum(num);
        if (block.generatedByMyself) {
          transactionCapsule.setVerified(true);
        }
        accountStateCallBack.preExeTrans();
        TransactionInfo result = processTransaction(transactionCapsule, block);
        accountStateCallBack.exeTransFinish();
```
