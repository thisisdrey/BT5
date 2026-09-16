### Title
Missing vote-reward settlement (reward "rebase") before Delegate/UnDelegate/FreezeBalanceV2/CancelAllUnfreezeV2 actuators mutate TronPower-affecting state - ([File: actuator/src/main/java/org/tron/core/actuator/DelegateResourceActuator.java], [File: actuator/src/main/java/org/tron/core/actuator/UnDelegateResourceActuator.java])

### Summary
The Yieldy bug class requires that a state-updating "rebase" happen before any function reads/mutates state that depends on the stale rate. In java-tron, the analogous "rebase" step is `MortgageService.withdrawReward()` / `VoteRewardUtil.withdrawReward()`, which settles a voter's accumulated reward against the account's TronPower/vote snapshot for the current and prior cycles before that snapshot is invalidated. `UnfreezeBalanceActuator`, `UnfreezeBalanceV2Actuator`, `VoteWitnessActuator`, and their TVM equivalents (`UnfreezeBalanceProcessor`, `UnfreezeBalanceV2Processor`, `VoteWitnessProcessor`) all correctly call this settlement first thing in `execute()`.

### Finding Description
`VoteRewardUtil.withdrawReward` / `MortgageService.withdrawReward` compute reward using `beginCycle`/`endCycle` bookkeeping and the account's current `votesList`/`VotesCapsule` snapshot [1](#0-0) . Because reward accrual is a function of TronPower/vote weight that existed during each cycle, any actuator that changes TronPower (via frozen/delegated balance) or clears votes must settle (withdraw) the reward for the stale weight *before* mutating it — exactly the "rebase before value-dependent function" pattern from the referenced report.

This pattern is followed correctly in the unfreeze/vote paths: `UnfreezeBalanceActuator.execute()` calls `mortgageService.withdrawReward(ownerAddress)` as its first action, before any balance/weight mutation [2](#0-1) , and `UnfreezeBalanceV2Actuator.execute()` does the same [3](#0-2) . The TVM native-contract equivalent, `UnfreezeBalanceProcessor.execute()`, additionally re-checks after the weight change whether the account's `usedTronPower` now exceeds the new TronPower, and if so explicitly calls `VoteRewardUtil.withdrawReward` again and clears votes before the mismatch can be exploited [4](#0-3) . `VoteWitnessProcessor.execute()` also settles reward first before clearing/overwriting the votes list [5](#0-4) .

A repo-wide search shows that `mortgageService.withdrawReward` / `VoteRewardUtil.withdrawReward` are referenced only in `WithdrawBalanceActuator`, `UnfreezeBalanceActuator`, `UnfreezeBalanceV2Actuator`, and `VoteWitnessActuator` (plus their TVM counterparts) — `DelegateResourceActuator`, `UnDelegateResourceActuator`, `FreezeBalanceV2Actuator`, and `CancelAllUnfreezeV2Actuator` contain no such call. `DelegateResourceActuator`/`UnDelegateResourceActuator` mutate an account's delegated/frozen balance, which directly changes `TronPower` (the weight underlying vote-reward accounting), yet — unlike the unfreeze path — they do not settle the outstanding reward or re-validate `usedTronPower` against the new TronPower before the weight change is committed.

### Impact Explanation
If an account's TronPower can be reduced (e.g., by un-delegating resource it previously delegated to itself, or delegating away frozen balance) without first calling the reward-settlement/rebase step and without the `usedTronPower > newTronPower` guard that `UnfreezeBalanceProcessor` applies, the account's stored vote/TronPower snapshot used by `VoteRewardUtil.computeReward` can become inconsistent with its actual weight for that cycle. This can lead to unbacked/incorrect reward accrual or loss of legitimately earned reward across a cycle boundary — a fund-accounting integrity issue reachable by any account holder through an ordinary `DelegateResourceContract`/`UnDelegateResourceContract` transaction.

### Likelihood Explanation
High: `DelegateResourceContract` and `UnDelegateResourceContract` are ordinary, unprivileged transactions available to any account with frozen/delegated balance and active votes, requiring no special permissions — the same reachability class as the original Yieldy `unstake`/`claim` bug (any user-triggered state-changing call).

### Recommendation
Audit `DelegateResourceActuator`, `UnDelegateResourceActuator` (and their TVM processors), `FreezeBalanceV2Actuator`, and `CancelAllUnfreezeV2Actuator` and, wherever they mutate TronPower/frozen or delegated balance for an account holding votes, call `mortgageService.withdrawReward(ownerAddress)` (or `VoteRewardUtil.withdrawReward`) first — mirroring `UnfreezeBalanceActuator`/`UnfreezeBalanceV2Actuator` — and replicate the `usedTronPower > newTronPower` vote-invalidation guard from `UnfreezeBalanceProcessor` so votes are re-settled and cleared consistently whenever TronPower decreases below the account's used vote weight.

### Proof of Concept
Not independently reproduced; conclusion is based on static comparison of actuator/processor code paths that correctly settle reward before weight mutation (`UnfreezeBalanceActuator`, `UnfreezeBalanceV2Actuator`, `UnfreezeBalanceProcessor`, `VoteWitnessProcessor`) versus the delegate/undelegate/freezeV2/cancel-unfreeze paths, which the codebase search shows contain no `withdrawReward`/`mortgageService` references. A background Devin session with full repository and test access would be needed to construct a concrete transaction sequence (freeze → vote → delegate away balance → wait a cycle → withdraw reward) and confirm the exact reward-accounting discrepancy at runtime.

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

**File:** actuator/src/main/java/org/tron/core/actuator/UnfreezeBalanceActuator.java (L71-76)
```java
    byte[] ownerAddress = unfreezeBalanceContract.getOwnerAddress().toByteArray();

    //
    mortgageService.withdrawReward(ownerAddress);

    AccountCapsule accountCapsule = accountStore.get(ownerAddress);
```

**File:** actuator/src/main/java/org/tron/core/actuator/UnfreezeBalanceV2Actuator.java (L69-75)
```java
    byte[] ownerAddress = unfreezeBalanceV2Contract.getOwnerAddress().toByteArray();
    long now = dynamicStore.getLatestBlockHeaderTimestamp();

    mortgageService.withdrawReward(ownerAddress);

    AccountCapsule accountCapsule = accountStore.get(ownerAddress);
    long unfreezeAmount = this.unfreezeExpire(accountCapsule, now);
```

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/UnfreezeBalanceProcessor.java (L205-223)
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
```

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/VoteWitnessProcessor.java (L39-52)
```java
  public void execute(VoteWitnessParam param, Repository repo) throws ContractExeException {
    byte[] ownerAddress = param.getVoterAddress();
    VoteRewardUtil.withdrawReward(ownerAddress, repo);

    AccountCapsule accountCapsule = repo.getAccount(ownerAddress);

    VotesCapsule votesCapsule = repo.getVotes(ownerAddress);
    if (votesCapsule == null) {
      votesCapsule = new VotesCapsule(ByteString.copyFrom(ownerAddress),
          accountCapsule.getVotesList());
    }

    accountCapsule.clearVotes();
    votesCapsule.clearNewVotes();
```
