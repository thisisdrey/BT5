### Title
NullPointerDereference in VMActuator.call() when a deployed contract has no bytecode - (File: actuator/src/main/java/org/tron/core/actuator/VMActuator.java)

### Summary
`VMActuator.call()`, invoked from `VMActuator.validate()` for every `TriggerSmartContract` transaction, only assigns the `program` field inside an `if (isNotEmpty(code))` block, but then unconditionally dereferences `program.getResult()` right after that block closes. If a contract exists in the contract store but its associated bytecode is empty/absent, `program` remains `null` and the call throws an unhandled `NullPointerException` instead of the expected `ContractValidateException`.

### Finding Description
In `call()`: [1](#0-0) 
`code` is fetched independently from `contract` metadata via `rootRepository.getCode(contractAddress)`, and `this.program` is only constructed inside `if (isNotEmpty(code))`. Immediately after the block ends, the code unconditionally does: [2](#0-1) 
`program.getResult().setContractAddress(contractAddress);` — if `code` was empty, `program` is still `null` here, causing an NPE.

This mirrors the CVE-2023-3316 bug class: an object (`TIFF*`/`program`) that is only fully initialized on a successful "open" path is later dereferenced unconditionally by a subsequent step (`TIFFClose`/`program.getResult()`), causing a NULL pointer dereference when the initialization path was skipped due to missing/failed resource setup (missing output file / missing contract bytecode).

The earlier check `deployedContract = rootRepository.getContract(contractAddress)` at line 500-504 only verifies an entry exists in the `ContractStore`; it does not guarantee the `CodeStore` has non-empty bytecode for that same address, since code and contract metadata are stored and looked up independently: [3](#0-2) 

`validate()` calls `call()` directly with no wrapping guard beyond `ContractValidateException`: [4](#0-3) 
An uncaught `NullPointerException` here is a `RuntimeException`, not a `ContractValidateException`, so it is not handled by the actuator's expected validation-failure path.

### Impact Explanation
Because `call()` runs during transaction validation for any `TriggerSmartContract` transaction targeting a contract address, an attacker able to construct or discover a contract address whose `CodeStore` entry is empty (while a `ContractStore`/`ContractCapsule` entry still exists) can trigger this NPE simply by broadcasting a normal trigger-contract transaction. Depending on how far up the call stack the NPE propagates uncaught (this could not be fully verified with the available tools due to index/time limits), this can result in:
- Rejection/exception during processing of an otherwise well-formed transaction (denial of service for that transaction path), or
- If propagated to the block-application/tx-execution loop in `Manager`/`TransactionTrace` without a catch-all, an unhandled exception during block processing, causing the node to halt or crash — a network-reachable DoS from a single signed transaction.

I was unable to fully confirm from the retrieved code whether every call path (e.g., `Manager.processTransaction` / `TransactionTrace.exec()`) wraps actuator `validate()`/`execute()` calls in a catch-all for generic `RuntimeException`; my search for that specific catch pattern in `TransactionTrace.java` returned no matches, meaning the exception may not be caught at that layer, but this needs to be confirmed by a follow-up deep dive.

### Likelihood Explanation
The precondition (a contract address recorded in `ContractStore` with no/empty code in `CodeStore`) is the key unresolved point — I could not find, in this pass, a definitive normal-user-controlled sequence of transactions (e.g., contract creation + self-destruct sequence, or a partial creation failure path) that reliably produces this exact split state. `MUtil`/`Repository` reference self-destruct/delete-contract logic that could plausibly desynchronize `ContractStore` and `CodeStore` entries, but I did not have iterations left to trace `MUtil.destroyContract` or `Repository.deleteContract` (or code paths using `ContractCapsule.getSmartContractFromTransaction` combined with `allowTvmConstantinople` toggling around contract creation, where `saveCode` may be skipped under certain flag combinations) to prove reachability with certainty.

### Recommendation
Move the `program.getResult().setContractAddress(...)` call inside the `if (isNotEmpty(code))` block (or add an explicit `if (program == null) { return; }` / throw a `ContractValidateException` guard) so `call()` never dereferences `program` when bytecode is missing, matching the existing pattern used by `create()`, which validates prerequisites before constructing `program`.

### Proof of Concept
Not fully reproducible with the tools available in this pass — a concrete PoC requires confirming a state where `rootRepository.getContract(contractAddress)` returns non-null while `rootRepository.getCode(contractAddress)` returns null/empty (e.g., via self-destruct semantics, TVM_CONSTANTINOPLE-gated `saveCode` skip during creation, or a race/edge case in contract deletion). This should be validated by a background agent with full repository access and test execution capability before treating this as a confirmed, exploitable vulnerability.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/VMActuator.java (L158-169)
```java
    switch (contractType.getNumber()) {
      case ContractType.TriggerSmartContract_VALUE:
        trxType = TrxType.TRX_CONTRACT_CALL_TYPE;
        call();
        break;
      case ContractType.CreateSmartContract_VALUE:
        trxType = TrxType.TRX_CONTRACT_CREATION_TYPE;
        create();
        break;
      default:
        throw new ContractValidateException("Unknown contract type");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/VMActuator.java (L498-568)
```java
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

**File:** actuator/src/main/java/org/tron/core/actuator/VMActuator.java (L569-571)
```java

    program.getResult().setContractAddress(contractAddress);
    //transfer from callerAddress to targetAddress according to callValue
```
