### Title
TRX/TRC10 sent via `CALL`/`CALLTOKEN` to a precompiled contract address is transferred but permanently unrecoverable - (File: `actuator/src/main/java/org/tron/core/vm/program/Program.java`)

### Summary
The DODO report describes a case where a user's `msg.value` is accepted and moved into a contract even though the code path taken does not use or refund it, permanently trapping the funds. The closest reachable analog in java-tron is `Program.callToPrecompiledAddress`, which unconditionally transfers the `endowment` (TRX) or TRC10 token value attached to a `CALL`/`CALLCODE`/`CALLTOKEN` targeting a precompiled contract address, before invoking the precompile logic that never spends, forwards, or refunds that value.

### Finding Description
When TVM executes a `CALL`-family opcode (`OperationActions.exeCall`), it resolves the destination via `PrecompiledContracts.getContractForAddress(codeAddress)`. If the address is a precompile (e.g. ECRecover, SHA256, and other builtin addresses), execution is routed to `Program.callToPrecompiledAddress` instead of `callToAddress`: [1](#0-0) 

Inside `callToPrecompiledAddress`, once the sender's balance check passes, the code unconditionally moves the endowment (TRX via `MUtil.transfer`, or TRC10 via direct balance mutation) into the precompile's context address *before* executing `contract.execute(data)`: [2](#0-1) 

Precompiled contract addresses are not real deployed `SmartContract` accounts with associated code or an owning private key — they are hard-coded dispatch targets implemented in `PrecompiledContracts.PrecompiledContract#execute`. That execute logic (e.g. `ECRecover`) only computes and returns output data; it contains no logic to forward, refund, or otherwise account for value received. Once `deposit.addBalance`/`addTokenBalance` credits the precompile's context address and `deposit.commit()` is called on success, the balance is committed to an address that:
- has no deployed contract code able to move the balance out, and
- has no corresponding private key that could ever sign a transaction to spend it.

This mirrors the audit finding's root cause exactly: value is accepted and moved by the platform code even though the destination/path taken cannot make use of it, resulting in permanent loss for the caller.

### Impact Explanation
Any contract logic invoking a precompiled address with an attached `value` (for example composability bugs, third-party contracts, or user error when constructing calldata/value for a low-level `call`/`delegatecall`) will have that TRX/TRC10 silently and irreversibly locked, since the balance is credited to an address nobody controls. This is a permanent freezing of user funds reachable through a normal `TriggerSmartContract` transaction that ends up executing a `CALL` with value against a precompiled address from within contract bytecode.

### Likelihood Explanation
Reaching this from an EOA requires that the executed bytecode (either the top-level contract or one it internally calls) performs a `CALL`/`CALLCODE`/`DELEGATECALL` with non-zero value or token value to a precompiled address. This can happen due to a bug in the calling contract, a malformed/attacker-crafted encoded address (e.g., an address confused with a precompile's low-numbered address), or generic low-level `.call{value: x}(...)` patterns common in Solidity. No special privilege is needed — a single signed `TriggerSmartContract` transaction that ultimately executes such a call is sufficient to trigger the loss. This class of issue is a known general EVM/TVM characteristic (also present in Ethereum) rather than a project-specific validation gap, which somewhat lowers its novelty/severity compared to the original application-level DODO bug.

### Recommendation
Reject (or refund) any non-zero `endowment`/`tokenValue` attached to `CALL`/`CALLCODE`/`DELEGATECALL`/`CALLTOKEN` operations that target a known precompiled contract address in `Program.callToPrecompiledAddress`, e.g. by pushing zero to the stack and refunding the caller's energy/value instead of transferring and committing it, unless the specific precompile is explicitly designed to receive and account for value.

### Proof of Concept
1. Deploy a contract with a function that performs a low-level call such as `address(0x01).call{value: 100}("")` (address `0x01` maps to the ECRecover-style precompile in `PrecompiledContracts.getContractForAddress`).
2. Trigger this function via a standard `TriggerSmartContract` transaction with sufficient TRX balance on the calling contract.
3. Observe: `OperationActions.exeCall` routes to `Program.callToPrecompiledAddress`; the endowment is deducted from the contract and added to the precompile's balance via `MUtil.transfer`/`addTokenBalance` (`Program.java:1712-1732`), `deposit.commit()` is called on success, and no mechanism exists in `PrecompiledContracts` to later move that balance out — the TRX is permanently stuck.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/OperationActions.java (L1046-1055)
```java
    PrecompiledContracts.PrecompiledContract contract =
        PrecompiledContracts.getContractForAddress(codeAddress);
    if (contract != null) {
      if (program.isConstantCall()) {
        contract =  PrecompiledContracts.getOptimizedContractForConstant(contract);
      }
      program.callToPrecompiledAddress(msg, contract);
    } else {
      program.callToAddress(msg);
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L1712-1732)
```java
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
