### Title
Permanent loss of TRX sent as callValue to precompiled/native contract addresses - (File: `actuator/src/main/java/org/tron/core/vm/program/Program.java`)

### Summary
`Program.callToPrecompiledAddress` unconditionally transfers any non-zero `endowment` (the TRX value attached to a `CALL`) to the target precompiled contract's address before invoking the precompiled logic, exactly as it does for normal contract calls. Precompiled/native contract addresses (e.g. `0x1`–`0xa`, and TRON-specific native addresses such as the vote, freeze-v2, and proof-verification precompiles) are fixed system addresses with no private key, no contract code, and no actuator path that can spend from them. Any TRX moved into these addresses is therefore permanently unrecoverable, which is the same root cause as the reported "payable function with no withdrawal path" bug class: value is accepted at a code path that has no corresponding exit.

### Finding Description
When the TVM interpreter executes a `CALL`/`CALLCODE`/`STATICCALL` opcode targeting a precompiled address, `OperationActions.exeCall` dispatches to `Program.callToPrecompiledAddress` [1](#0-0) .

Inside `callToPrecompiledAddress`, the endowment (`msg.getEndowment()`) is transferred from the sender to the precompiled `contextAddress` using `MUtil.transfer` whenever `endowment > 0`, with no check that the target is a "real" spendable account or that the precompiled contract supports receiving/forwarding value: [2](#0-1) 

The precompiled contract addresses are hardcoded fixed constants (`ecRecoverAddr`, `sha256Addr`, ..., `verifyMintProofAddr`, `merkleHashAddr`, native FreezeV2 precompiles, etc.) with no owning key and no corresponding actuator or opcode that debits from these addresses back to a user: [3](#0-2) 

After the transfer, the precompiled logic executes and, on success, the deposit (including the moved TRX balance) is committed regardless of whether the specific precompiled function had any use for or awareness of the value sent: [4](#0-3) 

This mirrors the reported bug class precisely: a code path (`depositAsset`/here, a `CALL` with value to a precompiled address) is capable of accepting funds, but there is no corresponding function, actuator, or opcode capable of moving that balance back out of the fixed system address, resulting in permanent loss of the transferred TRX.

### Impact Explanation
Any TRX intentionally or accidentally sent as `msg.value` in a `CALL` to a precompiled/native contract address (whether by a poorly written contract, a mistaken low-level call, or an attacker tricking a contract into forwarding value to such an address) becomes permanently and irrecoverably locked, since there is no owner key or actuator/opcode capable of withdrawing balance held at these fixed addresses. This is a permanent freezing-of-funds condition reachable by any unprivileged contract caller through a standard `TriggerSmartContract` transaction.

### Likelihood Explanation
Reachable directly from a single signed `TriggerSmartContract` transaction executing a Solidity `call.value(...)` (or equivalent low-level call) targeting a low, well-known address such as `0x1`–`0xa` or one of the TRON native precompile addresses. No special privilege, timing, or malicious-SR/witness/peer assumption is required — only knowledge of the precompile address constants, which are public and hardcoded in `PrecompiledContracts.java`.

### Recommendation
In `Program.callToPrecompiledAddress`, reject or refund calls that carry non-zero `endowment` targeting a precompiled contract address (i.e., treat precompiled contracts as inherently `nonpayable` unless a specific precompile is designed to accept and account for value), mirroring how EVM implementations generally disallow value transfers to precompiles that don't implement a corresponding withdrawal mechanism.

### Proof of Concept
1. Deploy a simple contract with a function that performs `target.call{value: N}(data)` where `target` is set to the fixed precompile address `0x0000000000000000000000000000000000000001` (ecRecover) or a TRON-native precompile address (e.g. `0x1000001` range).
2. Trigger the contract with `callValue = N` sun.
3. Observe `deposit.commit()` at `Program.java:1758` persists the balance transfer to the precompiled address's account entry in `AccountStore`.
4. Confirm there is no `WithdrawBalanceContract`, actuator, or TVM opcode capable of debiting the fixed precompile address back to any account — the `N` sun remains permanently stuck.

Note: I was unable to fully verify at the code level whether some other unseen validation elsewhere in the transaction-execution pipeline (outside `Program.java`/`PrecompiledContracts.java`) explicitly rejects non-zero-value calls to precompiled addresses before reaching this code; this could not be confirmed with the available search tools and index coverage. A Devin session with full repository access would be needed to rule this out definitively.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/OperationActions.java (L1046-1056)
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
    program.step();
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L1712-1722)
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
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L1752-1758)
```java
      Pair<Boolean, byte[]> out = contract.execute(data);

      if (out.getLeft()) { // success
        this.refundEnergy(msg.getEnergy().longValue() - requiredEnergy, CALL_PRE_COMPILED);
        this.stackPushOne();
        returnDataBuffer = out.getRight();
        deposit.commit();
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L133-160)
```java
  private static final DataWord ecRecoverAddr = new DataWord(
      "0000000000000000000000000000000000000000000000000000000000000001");
  private static final DataWord sha256Addr = new DataWord(
      "0000000000000000000000000000000000000000000000000000000000000002");
  private static final DataWord ripempd160Addr = new DataWord(
      "0000000000000000000000000000000000000000000000000000000000000003");
  private static final DataWord identityAddr = new DataWord(
      "0000000000000000000000000000000000000000000000000000000000000004");
  private static final DataWord modExpAddr = new DataWord(
      "0000000000000000000000000000000000000000000000000000000000000005");
  private static final DataWord altBN128AddAddr = new DataWord(
      "0000000000000000000000000000000000000000000000000000000000000006");
  private static final DataWord altBN128MulAddr = new DataWord(
      "0000000000000000000000000000000000000000000000000000000000000007");
  private static final DataWord altBN128PairingAddr = new DataWord(
      "0000000000000000000000000000000000000000000000000000000000000008");
  private static final DataWord batchValidateSignAddr = new DataWord(
      "0000000000000000000000000000000000000000000000000000000000000009");
  private static final DataWord validateMultiSignAddr = new DataWord(
      "000000000000000000000000000000000000000000000000000000000000000a");
  private static final DataWord verifyMintProofAddr = new DataWord(
      "0000000000000000000000000000000000000000000000000000000001000001");
  private static final DataWord verifyTransferProofAddr = new DataWord(
      "0000000000000000000000000000000000000000000000000000000001000002");
  private static final DataWord verifyBurnProofAddr = new DataWord(
      "0000000000000000000000000000000000000000000000000000000001000003");
  private static final DataWord merkleHashAddr = new DataWord(
      "0000000000000000000000000000000000000000000000000000000001000004");
```
