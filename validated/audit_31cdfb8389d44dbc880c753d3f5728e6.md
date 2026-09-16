### Title
`UnfreezeBalanceProcessor.execute()` conditionally skips `VoteRewardUtil.withdrawReward()` before mutating vote/tron-power state, causing incorrect vote-reward accounting - (File: `actuator/src/main/java/org/tron/core/vm/nativecontract/UnfreezeBalanceProcessor.java`)

### Summary
This is analogous to the Gondi `settleWithBuyout()` finding: a settlement/state-mutation path exists in parallel to the "normal" path, but it skips (or conditionally skips) the accounting hook that the normal path always calls before mutating balances tied to time-weighted rewards. In java-tron, TRX freezing/voting rewards are accounted for via `VoteRewardUtil.withdrawReward()` / `MortgageService.withdrawReward()`, which snapshots the account's current vote list against `beginCycle`/`endCycle` before any vote or tron-power state changes. Every actuator-based unfreeze/vote/withdraw path (`UnfreezeBalanceActuator`, `UnfreezeBalanceV2Actuator`, `VoteWitnessActuator`, `WithdrawBalanceActuator`) unconditionally calls this reward-withdraw function first. The TVM native `unfreezeBalance()` opcode path, `UnfreezeBalanceProcessor.execute()`, only calls `VoteRewardUtil.withdrawReward()` inside a conditional branch, meaning an unfreeze via a smart contract can clear/mutate the account's vote list without ever snapshotting/paying out the reward accrued for the votes prior to the change.

### Finding Description
`UnfreezeBalanceProcessor.execute()` mutates frozen/delegated balances and total resource weight unconditionally, but only calls `VoteRewardUtil.withdrawReward(ownerAddress, repo)` — followed by `accountCapsule.clearVotes()` / `votesCapsule.clearNewVotes()` — inside this guarded block: [1](#0-0) 

The condition is `VMConfig.allowTvmVote() && !accountCapsule.getVotesList().isEmpty()` and, further nested, `accountCapsule.getTronPower() < usedTronPower * TRX_PRECISION`. If the account still has enough remaining `tronPower` to cover its existing votes after the unfreeze, this branch is skipped entirely — `VoteRewardUtil.withdrawReward()` is never invoked for this unfreeze operation.

Contrast this with every other actuator that mutates frozen balance, tron power, or votes, all of which call the reward withdrawal function unconditionally, before any state mutation: [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) 

`VoteRewardUtil.withdrawReward()` is the function that snapshots the account's vote list at `beginCycle`/updates `beginCycle`/`endCycle` and pays out `getAllowance()` for accrued reward based on `DelegationStore` Vi deltas: [6](#0-5) 

Because this snapshot-and-pay step is what correctly attributes past-cycle reward to a specific vote distribution, skipping it before the account's frozen balance (and hence future tron power / vote-consistency checks) changes means a subsequent legitimate `withdrawReward()` call (from any other path) will compute rewards using a `beginCycle` snapshot that does not reflect the true vote history at the time it should have been settled. This desynchronizes the record used for the reward computation from the actual chronology of freeze/unfreeze/vote events, exactly as `settleWithBuyout()` desynchronized Gondi's Pool accounting from the true state of the loan by not invoking `loanLiquidation()`.

### Impact Explanation
This affects vote-reward payout correctness for accounts using the TVM native `unfreezeBalance` precompiled/native contract (invoked via smart contract, e.g. `Program.unfreezeBalance` style paths) combined with `allowTvmVote`. Depending on relative timing of subsequent `withdrawReward`/vote/unfreeze calls, this can result in a party earning less than the correct reward (funds effectively lost/frozen for the affected account) or, in adversarial sequencing, being credited more/less than deserved between multiple actors sharing a vote/reward cycle. This falls into the "unbacked balance"/"permanent freezing of funds" category since reward accounting for vote-based delegation is state used to compute real TRX allowance payouts credited to accounts.

### Likelihood Explanation
Reachable by any account that can call the native TVM freeze/vote/unfreeze functions from a smart contract (this is a documented TVM feature per `VMConfig.allowTvmVote()`), i.e., an ordinary contract-calling transaction from an unprivileged EOA. No special permission or validator role is required — only that `allowTvmVote` is enabled (which it is on networks supporting TVM voting) and that the account votes and unfreezes such that `tronPower >= usedTronPower` after the unfreeze (the common, non-edge case).

### Recommendation
Move `VoteRewardUtil.withdrawReward(ownerAddress, repo)` in `UnfreezeBalanceProcessor.execute()` out of the conditional block so it is called unconditionally before any balance/vote/tron-power mutation, mirroring `UnfreezeBalanceActuator`/`UnfreezeBalanceV2Actuator`/`VoteWitnessProcessor`. This ensures the vote-reward snapshot always reflects the account state immediately prior to the unfreeze, regardless of whether the vote list needs to be forcibly cleared afterward.

### Proof of Concept
Note: I was unable to run or fully trace an end-to-end test/transaction sequence to definitively confirm the resulting monetary delta given the limited tool access in this session — this should be validated with a Devin session that can execute the existing `VoteTest`/`DelegationServiceTest` suites and construct a concrete before/after balance diff.

1. Enable `allowTvmVote` (`VMConfig.allowTvmVote()`).
2. Deploy/trigger a contract that freezes balance and votes for a witness (as in `framework/src/test/java/org/tron/common/runtime/vm/VoteTest.java`), accruing tron power exactly equal to (or greater than) votes cast.
3. Let one or more reward cycles pass (`payRewardAndDoMaintenance`), so the account has unclaimed rewards for the current vote distribution tracked in `DelegationStore`.
4. Call the native `unfreezeBalance` opcode/native contract on a portion of frozen balance such that remaining `accountCapsule.getTronPower() >= usedTronPower * TRX_PRECISION` (i.e., votes remain valid) — this causes the `VoteRewardUtil.withdrawReward()` branch in `UnfreezeBalanceProcessor.execute()` to be skipped entirely: [1](#0-0) 
5. Compare the resulting `beginCycle`/`endCycle`/`allowance` state in `DelegationStore`/`AccountCapsule` against the equivalent actuator-driven flow (`UnfreezeBalanceActuator`) for the same sequence of events, which always calls `mortgageService.withdrawReward(ownerAddress)` first: [2](#0-1)

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/UnfreezeBalanceProcessor.java (L205-224)
```java
    if (VMConfig.allowTvmVote() && !accountCapsule.getVotesList().isEmpty()) {
      long usedTronPower = 0;
      for (Protocol.Vote vote : accountCapsule.getVotesList()) {
        usedTronPower += vote.getVoteCount();
      }
      if (accountCapsule.getTronPower() < usedTronPower * TRX_PRECISION) {
        VoteRewardUtil.withdrawReward(ownerAddress, repo);
        VotesCapsule votesCapsule = repo.getVotes(ownerAddress);
        accountCapsule = repo.getAccount(ownerAddress);
        if (votesCapsule == null) {
          votesCapsule = new VotesCapsule(ByteString.copyFrom(ownerAddress),
              accountCapsule.getVotesList());
        } else {
          votesCapsule.clearNewVotes();
        }
        accountCapsule.clearVotes();
        repo.updateVotes(ownerAddress, votesCapsule);
        repo.updateAccount(ownerAddress, accountCapsule);
      }
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/UnfreezeBalanceActuator.java (L71-76)
```java
    byte[] ownerAddress = unfreezeBalanceContract.getOwnerAddress().toByteArray();

    //
    mortgageService.withdrawReward(ownerAddress);

    AccountCapsule accountCapsule = accountStore.get(ownerAddress);
```

**File:** actuator/src/main/java/org/tron/core/actuator/UnfreezeBalanceV2Actuator.java (L69-76)
```java
    byte[] ownerAddress = unfreezeBalanceV2Contract.getOwnerAddress().toByteArray();
    long now = dynamicStore.getLatestBlockHeaderTimestamp();

    mortgageService.withdrawReward(ownerAddress);

    AccountCapsule accountCapsule = accountStore.get(ownerAddress);
    long unfreezeAmount = this.unfreezeExpire(accountCapsule, now);
    long unfreezeBalance = unfreezeBalanceV2Contract.getUnfreezeBalance();
```

**File:** actuator/src/main/java/org/tron/core/actuator/VoteWitnessActuator.java (L156-163)
```java
    byte[] ownerAddress = voteContract.getOwnerAddress().toByteArray();

    VotesCapsule votesCapsule;

    //
    mortgageService.withdrawReward(ownerAddress);

    AccountCapsule accountCapsule = accountStore.get(ownerAddress);
```

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/VoteWitnessProcessor.java (L39-43)
```java
  public void execute(VoteWitnessParam param, Repository repo) throws ContractExeException {
    byte[] ownerAddress = param.getVoterAddress();
    VoteRewardUtil.withdrawReward(ownerAddress, repo);

    AccountCapsule accountCapsule = repo.getAccount(ownerAddress);
```

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
