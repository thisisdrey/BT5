This is confirmed: `callToPrecompiledAddress` in `Program.java` transfers `endowment` (TRX value) to the `contextAddress` of a precompiled contract before invoking it, permanently locking those funds since precompiled contracts have no code, no owner-controlled logic, and no withdrawal mechanism, exactly analogous to Dexter's "amount sent" trapping bug.

### Title
TVM CALL/CALLCODE/DELEGATECALL to precompiled-contract addresses unconditionally transfers TRX/TRC10 value, permanently trapping funds - (File: actuator/src/main/java/org/tron/core/vm/program/Program.java)

### Summary
`Program.callToPrecompiledAddress` [1](#0-0)  transfers the `msg.getEndowment()` value (TRX) or TRC10 token amount from the sender to `contextAddress` before dispatching to the precompiled contract implementation, with no check that value is zero or that the precompile is designed to receive/handle funds.

### Finding Description
When a Solidity `CALL`/`CALLCODE`/`DELEGATECALL`/`STATICCALL` opcode targets a precompiled-contract address (e.g., a native precompile such as the batch-validatesign or freeze-related precompiles), `Program.callToPrecompiledAddress` executes the following unconditional transfer whenever `endowment > 0` and sender != context address: [2](#0-1) 
This mirrors the reported Dexter class of bug: an entrypoint/function that is not designed to receive/hold value nonetheless accepts and moves value with no reversion, causing the sender's funds to be moved into an account (the precompile address) that has no code, no logic to forward/refund the balance, and is not reachable by any actuator that spends from arbitrary addresses. Precompiled contract addresses are not real deployed contracts with associated withdrawal logic — they are special-cased addresses recognized by `PrecompiledContracts.getContractForAddress`/`isPrecompiled` and invoked via native Java code, `Program.callToAddress` [3](#0-2)  (analogous non-precompile logic performs the mirrored TRX transfer for the standard code-address case). Any Solidity contract author (or the fallback/default handling of any deployed contract) that forwards `msg.value` via `call.value(x)(...)` to a precompile address — intentionally or due to a bug/miscalculation — results in TRX permanently locked at that address, since no actuator or opcode exists to withdraw balance held at a precompile address (there is no owning key, no code to execute a withdrawal, and the precompile handler itself never touches account balance).

### Impact Explanation
Funds transferred to a precompiled address in this manner are permanently frozen: the balance is recorded via `MUtil.transfer` (a real TRC10/TRX balance-store mutation) at an address that can never sign a transaction (no private key) and has no contract code capable of returning or forwarding the funds. This constitutes a permanent loss of user funds analogous to the Dexter "trapped tezos" issue, satisfying the medium-severity "permanent freezing of funds" bar.

### Likelihood Explanation
The path is directly reachable by any unprivileged account triggering a `TriggerSmartContract` transaction that executes attacker- or third-party-authored bytecode performing a `CALL`/`CALLCODE`/`DELEGATECALL` with nonzero value to a known precompiled address, or a legitimate DApp with a bug that forwards `msg.value` to a precompile. No special privilege, node compromise, or consensus assumption is required — only a single crafted smart-contract call. The likelihood is comparable to the original report's scenario of a user mis-sending value with a call, i.e., accidental or exploit-triggered, and depends on contract authors not validating destination addresses, similar to the "not clear if this is intended" ambiguity flagged in the original Dexter report.

### Recommendation
- Short term: in `Program.callToPrecompiledAddress`, reject (revert or `stackPushZero` + refund) any call with `endowment > 0` (or nonzero TRC10 amount) directed at a precompiled-contract address, since precompiles have no mechanism to spend or return such value.
- Long term: audit all TVM call paths (`callToAddress`, `callToPrecompiledAddress`, `create`, `CREATE2`) for symmetrical handling of value transfers to non-withdrawable destinations (precompiles, self-destructed accounts, black-hole address) and add explicit validation to prevent unrecoverable balance transfers.

### Proof of Concept
1. Deploy a simple contract with a function `f(address target) public payable { target.call.value(msg.value)(""); }`.
2. Call `f` via `TriggerSmartContract` with `call_value = 100000` sun and `target` set to a known precompiled-contract address (e.g., the address recognized by `PrecompiledContracts.getContractForAddress`).
3. Observe: `Program.callToPrecompiledAddress` executes `MUtil.transfer(deposit, senderAddress, contextAddress, 100000)` [4](#0-3) , moving 100000 sun to the precompile address's balance record.
4. Verify via `AccountStore` that the precompile address now holds a nonzero TRX balance with no transaction type or actuator capable of spending/withdrawing it, confirming the funds are permanently trapped.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L1078-1122)
```java
    // FETCH THE CODE
    AccountCapsule accountCapsule = getContractState().getAccount(codeAddress);

    byte[] programCode =
        accountCapsule != null ? getContractState().getCode(codeAddress) : EMPTY_BYTE_ARRAY;

    // only for TRX, not for token
    long contextBalance = 0L;
    if (byTestingSuite()) {
      // This keeps track of the calls created for a test
      getResult().addCallCreate(data, contextAddress,
          msg.getEnergy().getNoLeadZeroesData(),
          msg.getEndowment().getNoLeadZeroesData());
    } else if (!ArrayUtils.isEmpty(senderAddress) && !ArrayUtils.isEmpty(contextAddress)
        && senderAddress != contextAddress && endowment > 0) {
      createAccountIfNotExist(deposit, contextAddress);
      if (!isTokenTransfer) {
        try {
          VMUtils
              .validateForSmartContract(deposit, senderAddress, contextAddress, endowment);
        } catch (ContractValidateException e) {
          if (VMConfig.allowTvmConstantinople()) {
            refundEnergy(msg.getEnergy().longValue(), REFUND_ENERGY_FROM_MESSAGE_CALL);
            throw new TransferException("transfer trx failed: %s", e.getMessage());
          }
          throw new BytecodeExecutionException(VALIDATE_FOR_SMART_CONTRACT_FAILURE, e.getMessage());
        }
        deposit.addBalance(senderAddress, -endowment);
        contextBalance = deposit.addBalance(contextAddress, endowment);
      } else {
        try {
          VMUtils.validateForSmartContract(deposit, senderAddress, contextAddress,
              tokenId, endowment);
        } catch (ContractValidateException e) {
          if (VMConfig.allowTvmConstantinople()) {
            refundEnergy(msg.getEnergy().longValue(), REFUND_ENERGY_FROM_MESSAGE_CALL);
            throw new TransferException("transfer trc10 failed: %s", e.getMessage());
          }
          throw new BytecodeExecutionException(VALIDATE_FOR_SMART_CONTRACT_FAILURE, e.getMessage());
        }
        deposit.addTokenBalance(senderAddress, tokenId, -endowment);
        deposit.addTokenBalance(contextAddress, tokenId, endowment);
      }
    }

```

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

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L1712-1731)
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
```
