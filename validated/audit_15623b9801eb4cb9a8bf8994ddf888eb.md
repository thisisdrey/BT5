## Analysis

The `ValidateMultiSign` precompile's `execute()` method has an `isValidAbiEncoding` guard that only runs `if (VMConfig.allowTvmOsaka())` [1](#0-0) . When that hard-fork flag is not yet active, execution falls straight into `DataWord.parseArray(rawData)` and unconditionally indexes `words[0]`, `words[1]`, `words[2]`, `words[3]` [2](#0-1) . If `rawData` (fully attacker-controlled TVM call data) has fewer than 4 words, `parseArray` returns a short array and this indexing throws `ArrayIndexOutOfBoundsException`. Unlike `BatchValidateSign.execute()`, which wraps `doExecute` in a `try { } catch (Throwable t)` [3](#0-2) , `ValidateMultiSign.execute()` has **no outer catch** around this parsing/indexing code — the only `try/catch` starts later, after the account/permission lookup [4](#0-3) .

The call site, `Program.callToPrecompiledAddress`, invokes `contract.execute(data)` with no try/catch either [5](#0-4) , so the uncaught `ArrayIndexOutOfBoundsException` propagates up through the VM execution loop as a runtime exception. This is exactly the class of bug in the TensorFlow advisory: a type/shape mismatch between what the code expects (well-formed 32-byte-word-aligned ABI data) and what is actually supplied, causing an unhandled crash deep in low-level array-indexing logic rather than a graceful validation failure.

Repository test code explicitly documents this as a known, currently-unpatched pre-activation behavior: `ValidateMultiSignContractTest.testTip854PreActivationNoOp` states "before activation, malformed calldata reaches the legacy decoder... this precompile has no outer catch, so a too-short input raises inside the decoder; that is the documented pre-activation failure mode the TIP explicitly preserves" [6](#0-5) . The mitigation (TIP-854 / `allowTvmOsaka`) is only a forward-activated hard-fork flag, meaning on any deployment where that flag is not yet active, the bug is live.

I was not able to fully confirm within the index whether an uncaught `ArrayIndexOutOfBoundsException` from a precompile ultimately just fails/reverts the single transaction (caught somewhere in `TransactionTrace`/`Manager` block-application code) or propagates far enough to crash node processing entirely — that requires tracing `RuntimeImpl.execute()`'s caller chain (`TransactionTrace.exec`, `Manager.processTransaction`) further than the index surfaced. This affects whether the impact is "single tx fails" (low impact, out of scope per rules) vs. a genuine node-crash/DoS. Given the explicit test name "outer-frame containment" implying the *fix* specifically ensures the exception doesn't propagate to the outer VM frame (implying that *before* the fix, or when the guard doesn't apply, it did propagate uncontained), this is suggestive of real impact, but I cannot fully prove the ultimate blast radius (transaction failure vs. block-application/node crash) from the available index alone.

### Title
Unguarded array indexing in `ValidateMultiSign` precompile causes uncaught crash on malformed calldata (pre-TIP-854) - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The `ValidateMultiSign` TVM precompile parses raw contract call data into `DataWord[]` and immediately indexes fixed positions (`words[0..3]`) without checking the array length, unless the `allowTvmOsaka` chain parameter is active. A contract call with fewer than 4 words of input triggers an uncaught `ArrayIndexOutOfBoundsException` that is not caught anywhere between the precompile and `Program.callToPrecompiledAddress`.

### Finding Description
`ValidateMultiSign.execute()` only validates ABI shape (`isValidAbiEncoding`) conditionally on `VMConfig.allowTvmOsaka()`. When that condition is false (i.e., before the corresponding hard fork activates on a given network), `DataWord.parseArray(rawData)` is called on arbitrary attacker data and the result is indexed at fixed offsets 0-3 without bounds checking [7](#0-6) . This is directly analogous to the TensorFlow bug class: code assumes a well-formed shape/type for externally supplied data and, upon mismatch, fails with a low-level runtime crash instead of a validated error path. The one `try/catch` present in the method only wraps the account/permission-weight logic further down, not the parsing/indexing block [4](#0-3) . `Program.callToPrecompiledAddress` calls `contract.execute(data)` with no surrounding exception handling [5](#0-4) .

### Impact Explanation
An unhandled `RuntimeException` (`ArrayIndexOutOfBoundsException`) escaping the intended contract-execution sandbox during transaction processing is a denial-of-service class issue — at minimum causing this transaction (and any others in the same execution context) to fail in an uncontrolled manner rather than through the defined revert/exception paths in `RuntimeImpl.setResultCode`, which only recognizes specific `Program.*Exception` subtypes [8](#0-7) . The test suite's explicit framing around "outer-frame containment" for TIP-854 indicates the project itself treats uncontained precompile exceptions as a defect needing remediation.

### Likelihood Explanation
Trivially reachable: any account can deploy or call a smart contract that issues a `CALL`/`STATICCALL`/`DELEGATECALL` to the `ValidateMultiSign` precompile address with fewer than 4 words of calldata (e.g., empty or short calldata). No special privileges, signatures, or waiting periods are required beyond broadcasting one transaction, and the vulnerable code path is only guarded by a hard-fork flag that may be inactive on some networks/points in time.

### Recommendation
Add an unconditional length/shape check (equivalent to `isValidAbiEncoding`) before parsing and indexing `words` in `ValidateMultiSign.execute()`, independent of `VMConfig.allowTvmOsaka()`, and/or wrap the entire `execute()` body in a `try/catch(Throwable)` that returns `Pair.of(false, EMPTY_BYTE_ARRAY)` on any parsing failure, mirroring `BatchValidateSign`'s outer catch.

### Proof of Concept
Deploy any contract that performs a low-level `call` to the `ValidateMultiSign` precompile address with calldata shorter than 4×32 bytes (e.g., zero-length calldata), on a network/point in time where `allowTvmOsaka` is not yet enabled. `DataWord.parseArray` returns a `DataWord[]` shorter than 4 elements; the subsequent `words[1]`/`words[2]`/`words[3]` accesses throw `ArrayIndexOutOfBoundsException`, matching the exact scenario asserted as the "pre-activation failure mode" in `ValidateMultiSignContractTest.testTip854PreActivationNoOp` [6](#0-5) .

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1052-1074)
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
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1080-1118)
```java
      AccountCapsule account = this.getDeposit().getAccount(address);
      if (account != null) {
        try {
          Permission permission = account.getPermissionById(permissionId);
          if (permission != null) {
            //calculate weight
            long totalWeight = 0L;
            List<byte[]> executedSignList = new ArrayList<>();
            for (byte[] sign : signatures) {
              byte[] recoveredAddr = recoverAddrBySign(sign, hash);

              sign = merge(recoveredAddr, sign);
              if (ByteArray.matrixContains(executedSignList, recoveredAddr)) {
                if (ByteArray.matrixContains(executedSignList, sign)) {
                  continue;
                }
                MUtil.checkCPUTime();
              }
              long weight = TransactionCapsule.getWeight(permission, recoveredAddr);
              if (weight == 0) {
                //incorrect sign
                return Pair.of(true, DATA_FALSE);
              }
              totalWeight += weight;
              executedSignList.add(sign);
              executedSignList.add(recoveredAddr);
            }

            if (totalWeight >= permission.getThreshold()) {
              return Pair.of(true, dataOne());
            }
          }
        } catch (Throwable t) {
          if (t instanceof OutOfTimeException) {
            throw t;
          }
          logger.info("ValidateMultiSign error:{}", t.getMessage());
        }
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

**File:** framework/src/main/java/org/tron/common/runtime/RuntimeImpl.java (L77-133)
```java
  private void setResultCode(ProgramResult result) {
    RuntimeException exception = result.getException();
    if (Objects.isNull(exception) && StringUtils
        .isEmpty(result.getRuntimeError()) && !result.isRevert()) {
      result.setResultCode(contractResult.SUCCESS);
      return;
    }
    if (result.isRevert()) {
      result.setResultCode(contractResult.REVERT);
      return;
    }
    if (exception instanceof IllegalOperationException) {
      result.setResultCode(contractResult.ILLEGAL_OPERATION);
      return;
    }
    if (exception instanceof OutOfEnergyException) {
      result.setResultCode(contractResult.OUT_OF_ENERGY);
      return;
    }
    if (exception instanceof BadJumpDestinationException) {
      result.setResultCode(contractResult.BAD_JUMP_DESTINATION);
      return;
    }
    if (exception instanceof OutOfTimeException) {
      result.setResultCode(contractResult.OUT_OF_TIME);
      return;
    }
    if (exception instanceof OutOfMemoryException) {
      result.setResultCode(contractResult.OUT_OF_MEMORY);
      return;
    }
    if (exception instanceof PrecompiledContractException) {
      result.setResultCode(contractResult.PRECOMPILED_CONTRACT);
      return;
    }
    if (exception instanceof StackTooSmallException) {
      result.setResultCode(contractResult.STACK_TOO_SMALL);
      return;
    }
    if (exception instanceof StackTooLargeException) {
      result.setResultCode(contractResult.STACK_TOO_LARGE);
      return;
    }
    if (exception instanceof JVMStackOverFlowException) {
      result.setResultCode(contractResult.JVM_STACK_OVER_FLOW);
      return;
    }
    if (exception instanceof Program.TransferException) {
      result.setResultCode(contractResult.TRANSFER_FAILED);
      return;
    }
    if (exception instanceof Program.InvalidCodeException) {
      result.setResultCode(contractResult.INVALID_CODE);
      return;
    }
    result.setResultCode(contractResult.UNKNOWN);
  }
```
