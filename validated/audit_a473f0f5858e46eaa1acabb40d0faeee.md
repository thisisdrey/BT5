### Title
Voting rewards ("allowance") accrued by smart-contract accounts become permanently unclaimable if the deployed bytecode never calls `withdrawreward()` - ([File: actuator/src/main/java/org/tron/core/vm/program/Program.java])

### Summary
Any TRON account — including a smart contract — that casts votes for witnesses accrues TRX rewards which are stored in the account's `allowance` field. For a normal (EOA) account this reward can always be pulled out via a signed `WithdrawBalanceContract` transaction. For a **contract account**, however, the only code path that can move `allowance` into `balance` is the TVM `withdrawreward()` opcode, and that opcode can only ever be executed *from within the contract's own bytecode* (`owner = getContextAddress()`). If the contract that voted was never written to expose a function calling this opcode, the reward is earned but can never be retrieved by anyone — not even the contract owner/deployer — exactly mirroring the reported bug class where TRX transferred into a contract has no owner-controlled withdrawal path.

### Finding Description
When TRX is frozen/staked and votes are cast for witnesses, either directly by an EOA (`VoteWitnessActuator`) or from inside a smart contract via the `VOTEWITNESS` TVM opcode (`OperationActions.voteWitnessAction` → `VoteWitnessProcessor.execute`), the voting account accumulates a periodic reward that is added to `AccountCapsule.allowance` via `VoteRewardUtil.adjustAllowance`/`withdrawReward`: [1](#0-0) 

For a normal account, this `allowance` can be swept into spendable `balance` via `WithdrawBalanceActuator`, which requires a **signed** `WithdrawBalanceContract` transaction from the account's private key: [2](#0-1) 

For a contract account there is no private key, so this signed-transaction path is unreachable. The only alternative is the TVM `withdrawreward()` native opcode, implemented in `Program.withdrawReward()`, which explicitly binds the operation to `getContextAddress()` — i.e., it can only withdraw the reward of the contract executing the opcode itself, and nothing else: [3](#0-2) 

Consequently, if a contract's Solidity source never includes a function that calls the `withdrawreward()` builtin (the pattern shown in the test fixture `Vote` contract at `framework/src/test/java/org/tron/common/runtime/vm/VoteTest.java`), then any reward accrued to that contract's `allowance` from voting is permanently unreachable — no other account, including the contract's deployer/owner, has any mechanism (signed transaction, opcode, or otherwise) to move it into a spendable balance. [4](#0-3) 

This is structurally identical to the reported issue: value legitimately accrues to a contract account (voting reward vs. ETH minting fee) but the contract has no exposed function to retrieve it, and the protocol provides no owner-privileged fallback withdrawal path analogous to `FootiumAcademy`'s `withdraw()`.

### Impact Explanation
Any smart contract that participates in on-chain governance/voting (a common DeFi/staking pattern — pooling frozen TRX and voting on behalf of depositors) but is deployed without a `withdrawreward()`-calling function will have its TRX voting rewards permanently and irrecoverably locked. Given that voting rewards accumulate every cycle indefinitely, the frozen amount can grow unbounded over time, representing a permanent freezing of funds for the contract owner/depositors with no possible remediation post-deployment (the bytecode cannot be upgraded after the fact for a non-proxy contract).

### Likelihood Explanation
This occurs on any contract deployed by an unprivileged contract deployer that (a) freezes TRX, (b) calls `VOTEWITNESS` to vote for witnesses, and (c) does not implement a public/external function invoking `WITHDRAWREWARD`. This is a realistic and easy-to-hit scenario for staking-pool style contracts, since exposing `withdrawreward()` is not enforced or warned about by the protocol, and omitting it silently causes rewards to accrue with no recovery path.

### Recommendation
Provide a protocol-level, permissionless mechanism to sweep a contract's accrued voting `allowance` into its own `balance` regardless of whether the deployed bytecode calls `withdrawreward()` — e.g., allow the actuator/opcode dispatch layer to expose withdrawal for any address whose reward is nonzero, or emit clear documentation/tooling warnings when a contract votes without implementing the withdrawal opcode, and consider allowing the account owner (via a signed transaction referencing the contract address, if permission model allows) to trigger reward withdrawal for contracts they control.

### Proof of Concept
1. Deploy a minimal contract `PoolNoWithdraw` that only implements `freeze()` (to gain Tron Power) and `voteWitness(address[], uint[])` (calling the `VOTEWITNESS` opcode), but does **not** implement any function calling `withdrawreward()`.
2. From an EOA, fund the contract, call `freeze()` then `voteWitness()` to vote for an active witness.
3. Advance several maintenance cycles (`payRewardAndDoMaintenance` in test harness, or wait for real maintenance intervals on a live network) so that `VoteRewardUtil` accrues reward into the contract's `AccountCapsule.allowance` (verifiable via `mortgageService.queryReward(contractAddress)` returning > 0, as exercised in `VoteTest.checkRewardAndWithdraw`).
4. Attempt to retrieve the reward:
   - Broadcasting `WithdrawBalanceContract` signed by any key fails validation/signature check because the contract address has no controlling private key.
   - Calling `withdrawreward()` from a *different* contract or EOA has no effect on `PoolNoWithdraw`'s allowance, since `Program.withdrawReward()` always uses `getContextAddress()` (the caller's own contract context), not an arbitrary target address.
5. The `allowance` on `PoolNoWithdraw`'s account remains permanently non-zero and unreachable, confirming funds are irrecoverably frozen.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/utils/VoteRewardUtil.java (L112-120)
```java
  private static void adjustAllowance(byte[] address, long amount, Repository repository) {
    if (amount <= 0) {
      return;
    }
    AccountCapsule accountCapsule = repository.getAccount(address);
    long allowance = accountCapsule.getAllowance();
    accountCapsule.setAllowance(allowance + amount);
    repository.updateAccount(accountCapsule.createDbKey(), accountCapsule);
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/WithdrawBalanceActuator.java (L98-119)
```java
    byte[] ownerAddress = withdrawBalanceContract.getOwnerAddress().toByteArray();
    if (!DecodeUtil.addressValid(ownerAddress)) {
      throw new ContractValidateException("Invalid address");
    }

    AccountCapsule accountCapsule = accountStore.get(ownerAddress);
    if (accountCapsule == null) {
      String readableOwnerAddress = StringUtil.createReadableString(ownerAddress);
      throw new ContractValidateException(
          ACCOUNT_EXCEPTION_STR + readableOwnerAddress + NOT_EXIST_STR);
    }

    String readableOwnerAddress = StringUtil.createReadableString(ownerAddress);

    boolean isGP = CommonParameter.getInstance()
        .getGenesisBlock().getWitnesses().stream().anyMatch(witness ->
            Arrays.equals(ownerAddress, witness.getAddress()));
    if (isGP) {
      throw new ContractValidateException(
          ACCOUNT_EXCEPTION_STR + readableOwnerAddress
              + "] is a guard representative and is not allowed to withdraw Balance");
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L2340-2359)
```java
  public long withdrawReward() {
    Repository repository = getContractState().newRepositoryChild();
    byte[] owner = getContextAddress();

    increaseNonce();
    InternalTransaction internalTx = addInternalTx(null, owner, owner, 0, null,
        "withdrawReward", nonce, null);

    WithdrawRewardParam param = new WithdrawRewardParam();
    param.setOwnerAddress(owner);
    param.setNowInMs(getTimestamp().longValue() * 1000);
    try {
      WithdrawRewardProcessor processor = new WithdrawRewardProcessor();
      processor.validate(param, repository);
      long allowance = processor.execute(param, repository);
      repository.commit();
      if (internalTx != null) {
        internalTx.setValue(allowance);
      }
      return allowance;
```

**File:** framework/src/test/java/org/tron/common/runtime/vm/VoteTest.java (L62-67)
```java
   *     function withdrawReward() external returns(uint) {
   *       return withdrawreward();
   *     }
   *     function queryRewardBalance() external view returns(uint) {
   *       return rewardBalance();
   *     }
```
