### Title
`getRewardBalance` (TVM `REWARDBALANCE` opcode) reports a reward amount that diverges from what `withdrawReward` (`WITHDRAWREWARD` opcode) actually pays out - ([File: actuator/src/main/java/org/tron/core/vm/utils/VoteRewardUtil.java])

### Summary
`VoteRewardUtil.queryReward()` and `VoteRewardUtil.withdrawReward()` are two independent re-implementations of the same voting-reward accounting logic — one used by the read-only `REWARDBALANCE` TVM opcode (`Program.getRewardBalance`) and one used by the state-changing `WITHDRAWREWARD` opcode/actuator. As in the original DAOsis finding (`isBuyed()` hard-coding assumptions not enforced by `buy()`), the two functions do not implement identical branch logic for the same cycle-accounting state machine, so the value a contract/user is told is "available" does not match what is actually transferred.

### Finding Description
`queryReward` (view path, reachable from any smart contract via the `REWARDBALANCE` opcode): [1](#0-0) 

`withdrawReward` (state-mutating path, reachable via the `WITHDRAWREWARD` opcode / `WithdrawRewardProcessor`): [2](#0-1) 

Both are exposed to the VM interpreter through `Program`: [3](#0-2) 

Comparing the two implementations line by line:
- `withdrawReward` has an extra early-return branch that `queryReward` does **not** have:
```
if (beginCycle == currentCycle) {
  AccountCapsule account = repository.getAccountVote(beginCycle, address);
  if (account != null) {
    return;                       // withdrawReward: pays nothing
  }
}
```
`queryReward` only special-cases `beginCycle > currentCycle` (returning just the allowance) and otherwise falls straight into the `beginCycle + 1 == endCycle` branch and the final `computeReward` call, without ever checking `beginCycle == currentCycle`.

As a result, in the state where `beginCycle == currentCycle` and an `AccountVote` snapshot already exists for `beginCycle`, `getRewardBalance()` can compute and return a non-zero reward figure (via `computeReward`/`accountCapsule.getAllowance()`), while a subsequent `WITHDRAWREWARD` call in the *same* state performs `return;` immediately and pays out nothing (no `adjustAllowance` is executed, `allowance` is left untouched).

This mirrors exactly the class of bug reported for DAOsis: a read-only helper (`isBuyed`/`queryReward`) encodes different, un-synchronized assumptions about internal counters/cycles than the actual state-mutating function (`buy`/`withdrawReward`) that is supposed to enforce/execute the same invariant.

### Impact Explanation
Any unprivileged deployed contract or an off-chain client building on top of it can call `REWARDBALANCE` to learn "you have X TRX of reward available" and then invoke `WITHDRAWREWARD` expecting to receive X, but receive `0` instead because of the diverging branch. Contracts or wallets that build monetary logic (e.g., auto-compounding, batched distribution, or accounting reconciliation) around the value returned by `getRewardBalance()` can be misled into performing on-chain financial operations (transfers, further staking, ledger updates) based on funds that were never actually credited, which can result in inconsistent internal accounting/inflated balances recorded off the true on-chain state, or missed reward withdrawals for the affected cycle. Because this executes inside consensus-critical native-contract code (`WithdrawRewardProcessor`/`VoteRewardUtil`) invoked by ordinary `TriggerSmartContract` transactions, the divergence is deterministically reproducible by any account.

### Likelihood Explanation
The divergent state (`beginCycle == currentCycle` with an existing `AccountVote` snapshot for that cycle) is a normal, frequently-reached state in the voting/reward cycle machine — it occurs whenever a reward-withdraw or vote-update already recorded an `AccountVote` for the account in the current maintenance cycle before the account calls `REWARDBALANCE`/`WITHDRAWREWARD` again in the same cycle. No special privileges are required; a single unprivileged contract deployer/caller triggering `REWARDBALANCE` then `WITHDRAWREWARD` in the same cycle is sufficient to observe the mismatch.

### Recommendation
Refactor `VoteRewardUtil.queryReward` and `VoteRewardUtil.withdrawReward` (and their chain-base counterpart `MortgageService.queryReward`/`withdrawReward`) to share a single reward-computation routine so that the "how much is currently withdrawable" query and the actual withdraw execution can never diverge. Specifically, add the same `beginCycle == currentCycle` / existing-`AccountVote` guard to `queryReward` that `withdrawReward` already has (returning the correct value, e.g. `accountCapsule.getAllowance()`, instead of a phantom reward for that branch), and add regression tests asserting `queryReward(address) == actual amount credited by withdrawReward(address)` across all cycle-boundary states.

### Proof of Concept
Not executable from the index alone — this requires driving the `DelegationStore`/cycle state (`beginCycle`, `endCycle`, `currentCycle`, and a stored `AccountVote` snapshot for `beginCycle == currentCycle`) via `VoteWitnessProcessor`/maintenance cycle transitions, then calling the `REWARDBALANCE` opcode followed by `WITHDRAWREWARD` from a deployed contract and diffing the reported vs. actually-credited amounts, as done in the existing test harness `framework/src/test/java/org/tron/common/runtime/vm/RewardBalanceTest.java` and `VoteTest.java` (`checkRewardAndWithdraw`), which already exercises both code paths but does not assert on the `beginCycle == currentCycle` divergence case specifically: [4](#0-3)

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/utils/VoteRewardUtil.java (L16-55)
```java
  public static void withdrawReward(byte[] address, Repository repository) {
    if (!VMConfig.allowTvmVote()) {
      return;
    }
    AccountCapsule accountCapsule = repository.getAccount(address);
    long beginCycle = repository.getBeginCycle(address);
    long endCycle = repository.getEndCycle(address);
    long currentCycle = repository.getDynamicPropertiesStore().getCurrentCycleNumber();
    long reward = 0;
    if (beginCycle > currentCycle || accountCapsule == null) {
      return;
    }
    if (beginCycle == currentCycle) {
      AccountCapsule account = repository.getAccountVote(beginCycle, address);
      if (account != null) {
        return;
      }
    }
    if (beginCycle + 1 == endCycle && beginCycle < currentCycle) {
      AccountCapsule account = repository.getAccountVote(beginCycle, address);
      if (account != null) {
        reward = computeReward(beginCycle, endCycle, account, repository);
        adjustAllowance(address, reward, repository);
        reward = 0;
      }
      beginCycle += 1;
    }
    endCycle = currentCycle;
    if (CollectionUtils.isEmpty(accountCapsule.getVotesList())) {
      repository.updateBeginCycle(address, endCycle + 1);
      return;
    }
    if (beginCycle < endCycle) {
      reward += computeReward(beginCycle, endCycle, accountCapsule, repository);
      adjustAllowance(address, reward, repository);
    }
    repository.updateBeginCycle(address, endCycle);
    repository.updateEndCycle(address, endCycle + 1);
    repository.updateAccountVote(address, endCycle, accountCapsule);
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/utils/VoteRewardUtil.java (L57-88)
```java
  public static long queryReward(byte[] address, Repository repository) {
    if (!VMConfig.allowTvmVote()) {
      return 0;
    }
    AccountCapsule accountCapsule = repository.getAccount(address);
    long beginCycle = repository.getBeginCycle(address);
    long endCycle = repository.getEndCycle(address);
    long currentCycle = repository.getDynamicPropertiesStore().getCurrentCycleNumber();
    long reward = 0;
    if (accountCapsule == null) {
      return 0;
    }
    if (beginCycle > currentCycle) {
      return accountCapsule.getAllowance();
    }
    //withdraw the latest cycle reward
    if (beginCycle + 1 == endCycle && beginCycle < currentCycle) {
      AccountCapsule account = repository.getAccountVote(beginCycle, address);
      if (account != null) {
        reward = computeReward(beginCycle, endCycle, account, repository);
      }
      beginCycle += 1;
    }
    endCycle = currentCycle;
    if (CollectionUtils.isEmpty(accountCapsule.getVotesList())) {
      return reward + accountCapsule.getAllowance();
    }
    if (beginCycle < endCycle) {
      reward += computeReward(beginCycle, endCycle, accountCapsule, repository);
    }
    return reward + accountCapsule.getAllowance();
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L1379-1382)
```java
  public DataWord getRewardBalance(DataWord address) {
    long rewardBalance = VoteRewardUtil.queryReward(address.toTronAddress(), getContractState());
    return new DataWord(rewardBalance);
  }
```

**File:** framework/src/test/java/org/tron/common/runtime/vm/VoteTest.java (L848-866)
```java
  private void checkRewardAndWithdraw(byte[] contract, boolean isZero) throws Exception {
    long rewardBySystem = mortgageService.queryReward(contract);
    long beginCycle = dbManager.getDelegationStore().getBeginCycle(contract);
    long currentCycle = dbManager.getDynamicPropertiesStore().getCurrentCycleNumber();
    long passedCycle = max(0, currentCycle - beginCycle,
        dbManager.getDynamicPropertiesStore().disableJavaLangMath());
    Assert.assertTrue(isZero ? rewardBySystem == 0 : rewardBySystem > 0);
    triggerContract(contract, SUCCESS,
        getConsumer(">=", rewardBySystem)
            .andThen(getConsumer("<=", rewardBySystem + passedCycle)),
        queryRewardBalanceMethod);

    long oldBalance = dbManager.getAccountStore().get(contract).getBalance();
    long rewardByContract = new DataWord(triggerContract(contract, SUCCESS,
        getConsumer(">=", rewardBySystem)
            .andThen(getConsumer("<=", rewardBySystem + passedCycle)),
        withdrawRewardMethod).getRuntime().getResult().getHReturn()).longValue();
    long newBalance = dbManager.getAccountStore().get(contract).getBalance();
    Assert.assertEquals(oldBalance + rewardByContract, newBalance);
```
