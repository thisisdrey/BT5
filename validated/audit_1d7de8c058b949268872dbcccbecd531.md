Based on my investigation, I found a strong analog in `callToPrecompiledAddress` within the TVM's `Program.java`, where the endowment (TRX value) transfer to a precompiled contract address is explicitly documented as irreversible even if the precompiled call subsequently fails.

### Title
Irreversible TRX Endowment Transfer to Precompiled Contract Addresses on Call Failure - (File: actuator/src/main/java/org/tron/core/vm/program/Program.java)

### Summary
`Program.callToPrecompiledAddress` charges the `msg.value`/endowment TRX transfer from the caller to the precompiled contract's context address directly via `MUtil.transfer` before the precompiled contract logic executes. The code comment explicitly states this charge "is not reversible by rollback." If the precompiled call subsequently fails (energy insufficient or execution returns failure), the value transfer is not refunded to the caller, unlike normal contract calls where the transfer is applied through a child `Repository` that is only committed on success.

### Finding Description
In `callToAddress` (regular contract calls), the endowment transfer is performed on a child `deposit` repository [1](#0-0) , and that repository's changes -- including the balance transfer -- are only persisted via `deposit.commit()` when the call succeeds; on exception or revert, the internal transaction is rejected and `deposit.commit()` is never called [2](#0-1) .

However, `callToPrecompiledAddress` applies the endowment charge directly and unconditionally before invoking the precompiled contract, with an explicit comment acknowledging non-reversibility: [3](#0-2) 

Subsequently, if the required energy for the precompiled call exceeds available energy, or the precompiled `contract.execute(data)` call returns failure, the stack is pushed zero and energy is spent/refunded, but there is no logic to reverse the TRX (or TRC10 token) balance already moved to the precompiled address: [4](#0-3) 

This is directly analogous to the `AvailBridge.sendMessage` bug class: value is taken from the caller ahead of the outcome being determined, and no compensating refund path exists when the "service" (message relay / precompiled call) does not actually complete successfully.

### Impact Explanation
A smart contract that performs a `CALL` with non-zero value to a precompiled contract address (e.g., via low-level `call.value(x)(...)`) and where the precompiled call subsequently fails (e.g., insufficient energy, precompiled `execute` returns `false`, or an exception is thrown before/at the precompiled step) will lose the transferred TRX/TRC10 permanently — the funds are moved to the precompiled address's balance record but the calling contract logic sees a failure (`stackPushZero()`), so from the caller's perspective the transfer had no effect except the balance loss. This is a concrete unauthorized loss of funds for any unprivileged account whose contract logic sends value to a precompiled address under conditions that can fail.

### Likelihood Explanation
Reachable from a single `TriggerSmartContractContract`/`TriggerConstantContract` transaction executed by any unprivileged account, as long as the calling contract issues a `CALL` opcode with value to a precompiled contract address and a failure condition is triggered (e.g., by deliberately setting a low gas stipend on the call, or by supplying malformed precompiled input causing `contract.execute` to return `false`). This requires crafting a contract that calls a precompile with value under an energy-starved sub-call, which is straightforward for a contract deployer/caller to construct deterministically.

### Recommendation
Move the endowment/token transfer logic in `callToPrecompiledAddress` onto the same child `Repository`/`deposit` pattern used in `callToAddress`, so that the transfer is only committed once the precompiled contract signals success (`out.getLeft() == true`), and rolled back automatically otherwise. Alternatively, add explicit refund logic that reverses the `MUtil.transfer`/token balance adjustments when `contract.execute(data)` returns failure or when the energy check fails.

### Proof of Concept
1. Deploy a contract that performs `someAddr.call.value(V)(data)` where `someAddr` is a precompiled contract address (any of TRON's precompiled contract slots), where `data` is crafted to make `contract.execute(data)` return `Pair.of(false, ...)`, or where the gas forwarded is deliberately insufficient (`requiredEnergy > msg.getEnergy().longValue()`).
2. Observe in `callToPrecompiledAddress`/`callToAddress` execution path: the endowment `V` is unconditionally transferred to the precompiled contextAddress balance via `MUtil.transfer` at [5](#0-4)  before the precompiled logic outcome is known.
3. Trigger the failure path (energy insufficient or `execute` returns false) at [6](#0-5) : the caller's contract receives `0` on the stack (failure signaled), but no code path restores `V` back to the caller — the balance has already moved and there is no revert of that specific transfer.
4. Confirm via `AccountStore` that the value moved to the precompiled address's balance persists despite the CALL "failing" from the caller contract's point of view, verifying permanent, unrefunded loss of funds analogous to the `AvailBridge.sendMessage` issue.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L1039-1052)
```java
    Repository deposit = getContractState().newRepositoryChild();

    // 2.1 PERFORM THE VALUE (endowment) PART
    long endowment;
    try {
      endowment = msg.getEndowment().value().longValueExact();
    } catch (ArithmeticException e) {
      if (VMConfig.allowTvmConstantinople()) {
        refundEnergy(msg.getEnergy().longValue(), "endowment out of long range");
        throw new TransferException("endowment out of long range");
      } else {
        throw e;
      }
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L1166-1194)
```java
      this.nonce = program.nonce;

      if (callResult.getException() != null || callResult.isRevert()) {
        logger.debug("contract run halted by Exception: contract: [{}], exception: [{}]",
            Hex.toHexString(contextAddress),
            callResult.getException());
        internalTx.reject();

        callResult.rejectInternalTransactions();

        stackPushZero();

        if (callResult.getException() != null) {
          return;
        }
      } else {
        // 4. THE FLAG OF SUCCESS IS ONE PUSHED INTO THE STACK
        deposit.commit();
        stackPushOne();
      }

      if (byTestingSuite()) {
        logger.debug("Testing run, skipping storage diff listener");
      }
    } else {
      // 4. THE FLAG OF SUCCESS IS ONE PUSHED INTO THE STACK
      deposit.commit();
      stackPushOne();
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L1704-1733)
```java
    if (senderBalance < endowment) {
      stackPushZero();
      refundEnergy(msg.getEnergy().longValue(), REFUND_ENERGY_FROM_MESSAGE_CALL);
      return;
    }
    byte[] data = this.memoryChunk(msg.getInDataOffs().intValue(),
        msg.getInDataSize().intValue());

    // Charge for endowment - is not reversible by rollback
    if (!ArrayUtils.isEmpty(senderAddress) && !ArrayUtils.isEmpty(contextAddress)
        && senderAddress != contextAddress && msg.getEndowment().value().longValueExact() > 0) {
      if (!isTokenTransfer) {
        try {
          MUtil.transfer(deposit, senderAddress, contextAddress,
              msg.getEndowment().value().longValueExact());
        } catch (ContractValidateException e) {
          throw new BytecodeExecutionException("transfer failure");
        }
      } else {
        try {
          VMUtils
              .validateForSmartContract(deposit, senderAddress, contextAddress, tokenId, endowment);
        } catch (ContractValidateException e) {
          throw new BytecodeExecutionException(VALIDATE_FOR_SMART_CONTRACT_FAILURE, e.getMessage());
        }
        deposit.addTokenBalance(senderAddress, tokenId, -endowment);
        deposit.addTokenBalance(contextAddress, tokenId, endowment);
      }
    }

```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L1734-1774)
```java
    long requiredEnergy = contract.getEnergyForData(data);
    if (requiredEnergy > msg.getEnergy().longValue()) {
      // Not need to throw an exception, method caller needn't know that
      // regard as consumed the energy
      this.refundEnergy(0, CALL_PRE_COMPILED); //matches cpp logic
      this.stackPushZero();
    } else {
      // Delegate or not. if is delegated, we will use msg sender, otherwise use contract address
      if (msg.getOpCode() == Op.DELEGATECALL) {
        contract.setCallerAddress(getCallerAddress().toTronAddress());
      } else {
        contract.setCallerAddress(getContextAddress());
      }
      // this is the depositImpl, not contractState as above
      contract.setRepository(deposit);
      contract.setResult(this.result);
      contract.setConstantCall(isConstantCall());
      contract.setVmShouldEndInUs(getVmShouldEndInUs());
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

      if (VMConfig.allowTvmSelfdestructRestriction()) {
        this.memorySave(msg.getOutDataOffs().intValueSafe(), msg.getOutDataSize().intValueSafe(), out.getRight());
      } else {
        this.memorySave(msg.getOutDataOffs().intValue(), out.getRight());
      }
    }
  }
```
