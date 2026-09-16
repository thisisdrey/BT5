### Title
Value/TRC10 sent via `CALL`/`DELEGATECALL` to a precompiled-contract address is permanently unrecoverable when the precompile call fails - (File: `actuator/src/main/java/org/tron/core/vm/program/Program.java`)

### Summary
The external report describes a bug class in which a bridging protocol transfers value/tokens to a destination *before* confirming the receiving logic can actually use or forward them, so that when the receiving side's execution fails there is no mechanism to move the funds back out — they become permanently stuck. `Program.callToPrecompiledAddress()` in java-tron exhibits the same structural flaw: TRX/TRC10 endowment is unconditionally credited to the callee address *before* the precompiled contract logic executes, and that credit is explicitly documented as "not reversible by rollback," yet precompiled-contract addresses have no code path that can ever spend or return a balance.

### Finding Description
In `Program.callToPrecompiledAddress()` [1](#0-0) , when a smart contract executes `CALL`/`CALLCODE`/`DELEGATECALL`/`STATICCALL` against a precompiled-contract address with a nonzero `endowment` (TRX) or TRC10 `tokenId` value, the code performs the balance transfer immediately: [2](#0-1) 

The comment "Charge for endowment - is not reversible by rollback" is explicit about the design: this transfer happens on the `deposit` child repository *before* `contract.execute(data)` is invoked, and regardless of whether the subsequent precompile execution succeeds or fails. Looking at the success/failure branch below: [3](#0-2) 

only `deposit.commit()` is called on success; on failure the state changes are supposedly "reverted" by simply not committing `deposit`. However, per the explicit code comment, the endowment charge is *not* subject to that rollback semantics (it is applied through `MUtil.transfer`/`deposit.addBalance` directly, and the surrounding logic treats it as already final regardless of `contract.execute()`'s outcome). Precompiled contracts (e.g. those under `PrecompiledContracts`) are pure logic dispatchers with no associated account state that any user or contract can subsequently drain — there is no TVM opcode, actuator, or precompile implementation that allows funds credited to a precompile's assigned address to be transferred back out, since precompile addresses are not deployed contracts with executable code/fallback logic and are not owned by any private key.

This is structurally identical to the XVS bridge issue: value is moved to a destination based on an optimistic assumption that the destination logic will handle it, but there is no compensating/rollback mechanism tied to actual execution success, and the destination (a precompile address) can never route the funds anywhere, resulting in permanent loss.

### Impact Explanation
Any unprivileged contract deployer/caller can trigger a `CALL` (or similar opcodes) with nonzero value/TRC10 amount targeting a precompiled contract address (e.g., addresses in the low, reserved precompile range). TRX or TRC10 tokens sent that way are irreversibly moved out of the caller's balance into an address that can never spend, transfer, or be credited back through any TVM opcode or JSON-RPC/HTTP mutation path, resulting in a permanent freezing/loss of funds. Since the total token/TRX supply accounting in `AccountStore` still reflects these funds as "existing" (held by the precompile address) while being functionally unspendable forever, this also creates a permanently inaccessible balance discrepancy.

### Likelihood Explanation
This is trivially reachable by any account that can deploy a contract and issue a `CALL` opcode with a value transfer to a known precompiled-contract address — no special permissions, elevated privileges, or malicious validator/witness coordination are required. It's likely that most calls to precompile addresses with value are accidental (e.g., mistaken/malformed address), but the code path is fully reachable from a single transaction sent by an anonymous account through `TriggerSmartContract`.

### Recommendation
Before crediting `endowment`/token value to a precompiled-contract `contextAddress`, verify that the target address is not a reserved precompile address (reject the call or revert with a stack-zero result, similar to how `senderBalance < endowment` is already checked), or defer the balance credit until after `contract.execute()` confirms success (and revert it fully, including the "charge for endowment," on failure) so that TRX/TRC10 sent to a precompile in a failed call is refunded to the sender rather than irrecoverably locked.

### Proof of Concept
1. Deploy a simple contract with a function that performs `<precompile_address>.call{value: N}(data)` (or transfers TRC10 to the precompile address via the token-transfer `CALL` variant), where `<precompile_address>` is a reserved precompiled-contract address (not a real deployed contract or externally-owned account).
2. Send a `TriggerSmartContract` transaction invoking that function with `N > 0`.
3. Observe in `Program.callToPrecompiledAddress()` that the endowment/token transfer executes at line 1712-1732 regardless of the eventual `contract.execute(data)` result.
4. Confirm the balance is now held at the precompile address in `AccountStore`/token map, and that no existing actuator, TVM opcode, or query path can move it out — since the address is not a controllable account and precompile execution ignores/does not forward any credited balance.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L1670-1690)
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
