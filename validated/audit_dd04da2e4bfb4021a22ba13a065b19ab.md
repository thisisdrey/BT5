## Title
Unpriced VM execution in `pushTransaction` allows cheap, repeated CPU-exhaustion DoS against a full node's broadcast path - (File: `framework/src/main/java/org/tron/core/db/Manager.java`)

## Summary
The Polygon zkEVM report describes attackers bypassing a cheap "would this revert" pre-check by manipulating a fee-related field, forcing the sequencer to repeatedly perform expensive full execution on transactions that were meant to be cheaply filtered, driving up computational load on sequencers/validators. The closest reachable analog in java-tron is the ordering of checks inside `Manager.processTransaction`, invoked from `Manager.pushTransaction` on every broadcast transaction: cheap structural/signature checks pass first, but full TVM execution (`trace.exec()`) runs before the transaction's economic cost is actually settled against the sender in a way that bounds resource use up front.

## Finding Description
`Wallet.broadcastTransaction` performs light checks (signature length, cache dedup, pending-queue size) and then calls `dbManager.pushTransaction(trx)`. [1](#0-0) 

`Manager.pushTransaction` validates the transaction's signature, then immediately calls `processTransaction(trx, null)` inside a synchronized block while holding `transactionLock`, i.e. full contract validation/execution happens serially for every accepted broadcast transaction before it's even placed in `pendingTransactions`. [2](#0-1) 

Inside `processTransaction`, the order is: `validateTapos` → `validateCommon` → `validateDup` → signature check (already done) → `consumeBandwidth`/`consumeMultiSignFee`/`consumeMemoFee` → then `trace.init(...)`, `trace.checkIsConstant()`, and `trace.exec()`, which performs the actual TVM execution (including `VMActuator.call()`/`create()` for smart-contract invocations). [3](#0-2) 

Energy accounting for that execution (`payEnergyBill`) only happens afterward, as part of `trace.finalization()`/`pay()`, and a caller only needs a small non-zero balance/energy allowance to be accepted into `consumeBandwidth`—it does not need to actually afford the full VM run up front. `VMActuator.call`/`create` derive `energyLimit` from the caller's fee limit/frozen energy at execution time, but the *execution itself* (opcode interpretation, storage reads inside `TVM`) is what costs the node CPU regardless of whether the transaction ultimately succeeds, fails, or is later evicted by `PendingManager` timeouts. [4](#0-3) 

Because `pushTransaction` is reachable from any unprivileged, unauthenticated `broadcastTransaction` gRPC/HTTP call, and because the full validate+execute path runs synchronously under a single `transactionLock` for every accepted transaction (limited only by `isTooManyPending`/dedup cache, not by aggregate CPU/energy budget of the submitter), an attacker who funds many low-balance accounts (each satisfying minimal bandwidth/fee checks) can submit many transactions that trigger real TVM execution cycles on the node before ultimately failing on economic grounds or expiring from the pool, similar in spirit to the reported "low-cost claims causing DoS on sequencers/validators by increasing computational overhead" pattern.

## Impact Explanation
Sustained submission of many transactions that pass minimal signature/bandwidth checks but trigger real TVM execution can consume CPU time on a full node's transaction-processing thread (bounded by `transactionLock` serialization), degrading the node's ability to process legitimate transactions/blocks in a timely manner — a node availability/performance impact, not a fund-theft one. This matches the "resource exhaustion on validators/sequencers" class from the source report, scoped to unprivileged broadcasters interacting with `Wallet.broadcastTransaction` → `Manager.pushTransaction`.

## Likelihood Explanation
Reaching this path requires only a signed transaction with valid signature and enough balance/bandwidth to pass `consumeBandwidth`; no privileged role is needed. However, java-tron already has some mitigations not present in the zkEVM report — `isTooManyPending` queue-size limits, a `transactionIdCache` dedup cache, and per-account bandwidth/energy accounting that limits how many contract calls a single account can cheaply issue — which reduce (but do not eliminate) the practicality of large-scale amplification compared to the original zkEVM bug, since an attacker still needs many distinct funded accounts to bypass per-account throttling.

## Recommendation
- Consider adding a cheap pre-execution admission check (e.g., a fast estimate/reject of obviously unaffordable or trivially-failing contract calls) before invoking `trace.exec()` in `Manager.processTransaction`, so that TVM execution is only performed once basic affordability is more strongly assured.
- Evaluate rate-limiting/prioritizing broadcast-triggered contract-call transactions per source account within `pushTransaction`, independent of the existing `isTooManyPending` global queue cap.
- Confirm whether `transactionLock` serialization together with `maxTransactionPendingSize` already bounds worst-case CPU amplification to an acceptable level for the target deployment configurations; if so, downgrade this to a monitoring/tuning recommendation rather than a code defect.

## Proof of Concept
1. Fund N low-balance accounts, each with just enough TRX to pass `consumeBandwidth` for a `TriggerSmartContract` call to an existing contract with a moderately expensive function.
2. From each account, broadcast a `TriggerSmartContract` transaction via `Wallet.broadcastTransaction` with feeLimit set low enough that it will ultimately fail/partial-fail on energy exhaustion, but non-zero so it passes the `feeLimit < 0` check in `VMActuator.call`. [5](#0-4) 
3. Repeat concurrently from many accounts up to the `maxTransactionPendingSize`/`isTooManyPending` limit, observing CPU time spent in `Manager.processTransaction`/`trace.exec()` under `PROCESS_TRANSACTION_LATENCY` metrics versus the number of transactions that ultimately post no lasting state change.

Note: I was unable to fully verify with a live benchmark whether existing bandwidth/energy affordability gates before `trace.exec()` sufficiently bound the amplification factor in practice (i.e., how many low-cost executions a single funded account can trigger before being throttled), since that requires runtime profiling data not available via static code search alone.

### Citations

**File:** framework/src/main/java/org/tron/core/Wallet.java (L558-575)
```java
      if (trxCacheEnable) {
        if (dbManager.getTransactionIdCache().getIfPresent(txID) != null) {
          logger.warn("Broadcast transaction {} has failed, it already exists.", txID);
          return builder.setResult(false).setCode(response_code.DUP_TRANSACTION_ERROR)
              .setMessage(ByteString.copyFromUtf8("Transaction already exists.")).build();
        } else {
          dbManager.getTransactionIdCache().put(txID, true);
        }
      }

      if (chainBaseManager.getDynamicPropertiesStore().supportVM()) {
        trx.resetResult();
      }
      if (trx.getInstance().getRawData().getContractCount() == 0) {
        throw new ContractValidateException(ActuatorConstant.CONTRACT_NOT_EXIST);
      }
      trx.checkExpiration(chainBaseManager.getNextBlockSlotTime());
      dbManager.pushTransaction(trx);
```

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L904-944)
```java
    try {
      if (!trx.validateSignature(chainBaseManager.getAccountStore(),
          chainBaseManager.getDynamicPropertiesStore())) {
        throw new ValidateSignatureException(String.format("trans sig validate failed, id: %s",
            trx.getTransactionId()));
      }

      synchronized (transactionLock) {
        while (true) {
          try {
            if (isBlockWaitingLock()) {
              TimeUnit.MILLISECONDS.sleep(SLEEP_FOR_WAIT_LOCK);
            } else {
              break;
            }
          } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            logger.debug("The wait has been interrupted.");
          }
        }
        synchronized (this) {
          if (isShieldedTransaction(trx.getInstance())
                  && shieldedTransInPendingCounts.get() >= shieldedTransInPendingMaxCounts) {
            return false;
          }
          if (!session.valid()) {
            session.setValue(revokingStore.buildSession());
          }

          try (ISession tmpSession = revokingStore.buildSession()) {
            processTransaction(trx, null);
            trx.setTrxTrace(null);
            pendingTransactions.add(trx);
            Metrics.gaugeInc(MetricKeys.Gauge.MANAGER_QUEUE, 1,
                    MetricLabels.Gauge.QUEUE_PENDING);
            tmpSession.merge();
          }
          if (isShieldedTransaction(trx.getInstance())) {
            shieldedTransInPendingCounts.incrementAndGet();
          }
        }
```

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L1537-1561)
```java
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

**File:** actuator/src/main/java/org/tron/core/actuator/VMActuator.java (L481-568)
```java
  private void call()
      throws ContractValidateException {

    if (!rootRepository.getDynamicPropertiesStore().supportVM()) {
      logger.info("vm work is off, need to be opened by the committee");
      throw new ContractValidateException("VM work is off, need to be opened by the committee");
    }

    TriggerSmartContract contract = ContractCapsule.getTriggerContractFromTransaction(trx);
    if (contract == null) {
      return;
    }

    if (contract.getContractAddress() == null) {
      throw new ContractValidateException("Cannot get contract address from TriggerContract");
    }

    byte[] contractAddress = contract.getContractAddress().toByteArray();

    ContractCapsule deployedContract = rootRepository.getContract(contractAddress);
    if (null == deployedContract) {
      logger.info("No contract or not a smart contract");
      throw new ContractValidateException("No contract or not a smart contract");
    }

    long callValue = contract.getCallValue();
    long tokenValue = 0;
    long tokenId = 0;
    if (VMConfig.allowTvmTransferTrc10()) {
      tokenValue = contract.getCallTokenValue();
      tokenId = contract.getTokenId();
    }

    if (StorageUtils.getEnergyLimitHardFork()) {
      if (callValue < 0) {
        throw new ContractValidateException("callValue must be >= 0");
      }
      if (tokenValue < 0) {
        throw new ContractValidateException("tokenValue must be >= 0");
      }
    }

    byte[] callerAddress = contract.getOwnerAddress().toByteArray();
    checkTokenValueAndId(tokenValue, tokenId);

    byte[] code = rootRepository.getCode(contractAddress);
    if (isNotEmpty(code)) {
      long feeLimit = trx.getRawData().getFeeLimit();
      if (feeLimit < 0 || feeLimit > rootRepository.getDynamicPropertiesStore().getMaxFeeLimit()) {
        logger.info("invalid feeLimit {}", feeLimit);
        throw new ContractValidateException("feeLimit must be >= 0 and <= "
            + rootRepository.getDynamicPropertiesStore().getMaxFeeLimit());
      }
      AccountCapsule caller = rootRepository.getAccount(callerAddress);
      long energyLimit;
      if (isConstantCall) {
        energyLimit = maxEnergyLimit;
      } else {
        AccountCapsule creator = rootRepository
            .getAccount(deployedContract.getInstance().getOriginAddress().toByteArray());
        energyLimit = getTotalEnergyLimit(creator, caller, contract, feeLimit, callValue);
      }

      long thisTxCPULimitInUs = calculateCpuLimitInUs(isConstantCall,
          rootRepository.getDynamicPropertiesStore().getMaxCpuTimeOfOneTx(),
          getCpuLimitInUsRatio(), CommonParameter.getInstance().getConstantCallTimeoutMs());
      long vmStartInUs = System.nanoTime() / VMConstant.ONE_THOUSAND;
      long vmShouldEndInUs = vmStartInUs + thisTxCPULimitInUs;
      ProgramInvoke programInvoke = ProgramInvokeFactory
          .createProgramInvoke(TrxType.TRX_CONTRACT_CALL_TYPE, executorType, trx,
              tokenValue, tokenId, blockCap.getInstance(), rootRepository, vmStartInUs,
              vmShouldEndInUs, energyLimit);
      if (isConstantCall) {
        programInvoke.setConstantCall();
      }
      rootInternalTx = new InternalTransaction(trx, trxType);
      this.program = new Program(code, contractAddress, programInvoke, rootInternalTx);
      if (VMConfig.allowTvmCompatibleEvm()) {
        this.program.setContractVersion(deployedContract.getContractVersion());
      }
      byte[] txId = TransactionUtil.getTransactionId(trx).getBytes();
      this.program.setRootTransactionId(txId);

      if (enableEventListener && isCheckTransaction()) {
        logInfoTriggerParser = new LogInfoTriggerParser(blockCap.getNum(), blockCap.getTimeStamp(),
            txId, callerAddress);
      }
    }
```
