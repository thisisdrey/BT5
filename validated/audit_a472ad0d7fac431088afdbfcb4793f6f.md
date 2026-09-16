Confirmed: in `call()`, `this.program` is only assigned inside `if (isNotEmpty(code))` [1](#0-0) . When the target contract address has no code (`code` empty/null — e.g. a `TriggerSmartContract` sent to a non-existent or non-contract address, or a suicided contract), `program` stays `null`, and `execute()` falls into the `else` branch that calls `rootRepository.commit()` directly [2](#0-1) . If that call (or any other statement reached while `program == null`) throws, control goes to the generic `catch (Throwable e)` handler, which unconditionally calls `program.spendAllEnergy()` [3](#0-2)  without checking for null — mirroring the exact bug class in CVE-2021-47486, where a cleanup/finalization routine is invoked on a resource that was never initialized because the normal path was skipped.

### Title
Unconditional `program.spendAllEnergy()` in `VMActuator.execute()` catch block causes NullPointerException when `program` was never assigned - (File: actuator/src/main/java/org/tron/core/actuator/VMActuator.java)

### Summary
`VMActuator.execute()` guards the main VM interpretation path with `if (program != null)` [4](#0-3) , but the three `catch` blocks that follow (`JVMStackOverFlowException`, `OutOfTimeException`, and generic `Throwable`) call `program.spendAllEnergy()` unconditionally [5](#0-4) , with no null check, analogous to `bpf_jit_binary_free()` being called on a possibly-NULL `jit_data->header` in the referenced kernel CVE.

### Finding Description
`program` is a field populated only in `call()`'s `if (isNotEmpty(code))` branch [6](#0-5) ; if the target address has no deployed code, `program` remains `null` for the rest of `validate()`/`execute()`. In `execute()`, when `program == null` the code takes the `else` branch and calls `rootRepository.commit()` directly [2](#0-1) . Should that call (or any other statement executed while `program` is `null`, e.g. iterating `result.getDeleteAccounts()`) throw any `RuntimeException`/`Throwable` not already caught, execution reaches `catch (Throwable e)`, which does `program.spendAllEnergy()` before checking whether `program` is null [3](#0-2) . This throws a `NullPointerException`, which is not caught anywhere else in this method and propagates upward through `VMActuator.execute()` into the transaction-processing chain (`RuntimeImpl.execute()` → `TransactionTrace.exec()` → block application in `Manager`) [7](#0-6) [8](#0-7) .

### Impact Explanation
An uncaught `NullPointerException` thrown while a node is applying a transaction inside `pushBlock`/`applyBlock` is not gracefully handled at this layer; depending on how far up the call stack it propagates before being swallowed, this can abort block application, crash the transaction-processing thread, or cause the affected node to diverge/halt while other nodes (whose repository commit didn't fail, or which are on a different code path) continue normally — a broadcaster-triggerable denial-of-service against any node that processes the crafted transaction.

### Likelihood Explanation
Reaching this code path only requires broadcasting a `TriggerSmartContract` transaction addressed at an account with no code (trivial and unprivileged) so that `program` remains `null`. The remaining requirement — an exception being thrown from `rootRepository.commit()` (or another statement in the `program == null` branch) — is the main uncertainty: I was not able to positively confirm a concrete state in `RepositoryImpl.commit()` that throws under normal conditions from this index alone, so full exploitability could not be conclusively proven with the tools available.

### Recommendation
Guard each `catch` block in `VMActuator.execute()` with a null check before calling `program.spendAllEnergy()`, e.g. `if (program != null && !(e instanceof TransferException)) { program.spendAllEnergy(); }`, matching the intent of the CVE-2021-47486 fix (check for null before invoking the cleanup routine).

### Proof of Concept
1. Broadcast a `TriggerSmartContract` transaction whose `contract_address` points to an account that has no deployed bytecode (or a self-destructed contract) but exists and passes `call()`'s earlier checks, so `rootRepository.getCode(contractAddress)` returns empty and `program` is never assigned [9](#0-8) .
2. During `execute()`, since `program == null`, the `else` branch calls `rootRepository.commit()` [2](#0-1) .
3. If any exception is thrown from that call (this step needs further verification against the current `RepositoryImpl.commit()` implementation, which I could not fully inspect within this session), the generic `catch (Throwable e)` handler executes `program.spendAllEnergy()` on a null `program` reference [3](#0-2) , throwing `NullPointerException` that propagates out of `execute()`.

Note: due to index size limits, I could not fully inspect `RepositoryImpl.commit()` to confirm a concrete throwing condition; a Devin session with full repo access would be needed to verify whether step 3 is reachable in practice, or to identify another statement in the `program == null` branch that can throw.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/VMActuator.java (L180-181)
```java
    try {
      if (program != null) {
```

**File:** actuator/src/main/java/org/tron/core/actuator/VMActuator.java (L265-267)
```java
      } else {
        rootRepository.commit();
      }
```

**File:** actuator/src/main/java/org/tron/core/actuator/VMActuator.java (L271-290)
```java
    } catch (JVMStackOverFlowException e) {
      program.spendAllEnergy();
      result = program.getResult();
      result.setException(e);
      result.rejectInternalTransactions();
      clearExceptionResult(result);
      result.setRuntimeError(result.getException().getMessage());
      logger.info("JVMStackOverFlowException: {}", result.getException().getMessage());
    } catch (OutOfTimeException e) {
      program.spendAllEnergy();
      result = program.getResult();
      result.setException(e);
      result.rejectInternalTransactions();
      clearExceptionResult(result);
      result.setRuntimeError(result.getException().getMessage());
      logger.info("timeout: {}", result.getException().getMessage());
    } catch (Throwable e) {
      if (!(e instanceof TransferException)) {
        program.spendAllEnergy();
      }
```

**File:** actuator/src/main/java/org/tron/core/actuator/VMActuator.java (L526-560)
```java
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
```

**File:** framework/src/main/java/org/tron/common/runtime/RuntimeImpl.java (L52-60)
```java
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

**File:** chainbase/src/main/java/org/tron/core/db/TransactionTrace.java (L186-191)
```java
  public void exec()
      throws ContractExeException, ContractValidateException, VMIllegalException {
    /*  VM execute  */
    runtime.execute(transactionContext);
    setBill(transactionContext.getProgramResult().getEnergyUsed());
    setPenalty(transactionContext.getProgramResult().getEnergyPenaltyTotal());
```
