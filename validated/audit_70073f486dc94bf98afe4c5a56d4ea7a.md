### Title
Self-destructing a contract that has voted permanently forfeits its pending (in-progress-cycle) SR voting reward - (File: `actuator/src/main/java/org/tron/core/vm/program/Program.java`)

### Summary
When a smart contract that has cast SR votes (via the TVM vote precompile) executes `SELFDESTRUCT`, `Program.suicide()` calls `withdrawRewardAndCancelVote()`, which settles only the *already-completed* voting cycles and then immediately clears the account's vote list and destroys the account. The share of reward accrued for the still-in-progress current cycle is snapshotted into `DelegationStore` keyed to the (about-to-be-destroyed) contract address, but that snapshot can only ever be paid out through a future call to `VoteRewardUtil.withdrawReward()`/`MortgageService.withdrawReward()`, which require the account to still exist. Because the contract is destroyed, no future transaction can ever trigger that withdrawal, so this last-cycle reward becomes permanently unclaimable — the exact same "destroy governance/claim rights while a claimable share is in flight" pattern described in the rageQuit report.

### Finding Description
In `Program.suicide()`, when `VMConfig.allowTvmVote()` is enabled, the code calls: [1](#0-0) 

which invokes: [2](#0-1) 

`withdrawRewardAndCancelVote` calls `VoteRewardUtil.withdrawReward(owner, repo)` and then unconditionally clears `ownerCapsule`'s votes. Looking at `VoteRewardUtil.withdrawReward`: [3](#0-2) 

The function only pays out reward for cycles strictly before `currentCycle` (the `beginCycle + 1 == endCycle && beginCycle < currentCycle` branch, and the `beginCycle < endCycle` branch where `endCycle` is set to `currentCycle`, i.e. up to but excluding the in-progress cycle). It then stores a fresh snapshot of the account's (still non-cleared, at that point) vote list via `repository.updateAccountVote(address, endCycle, accountCapsule)` with `endCycle == currentCycle`, meaning this snapshot represents the vote state that must be used to compute the *next* reward once the current cycle completes.

Back in `withdrawRewardAndCancelVote`, immediately after this, `ownerCapsule.clearVotes()` is executed and the account is subsequently marked for self-destruction (`getContractState().markSelfDestruct(owner)`), so the address is destroyed at the end of the transaction. Any TRX reward corresponding to the `currentCycle` snapshot recorded in `DelegationStore` can only ever be realized by a subsequent call to `VoteRewardUtil.withdrawReward`/`MortgageService.withdrawReward`, both of which require `accountStore.get(address)`/`repository.getAccount(address)` to return a non-null account: [4](#0-3) 

Since the contract account has been self-destructed, no future actuator invocation from that address can ever occur, so the in-progress-cycle reward snapshot becomes permanently unreachable/unclaimable — analogous to `rageQuit()` burning the governance NFT while a claimable `TokenDistributor` share still exists.

### Impact Explanation
This causes a portion of TRX (SR voting reward) allocated for the current voting cycle to become permanently locked/unclaimable: it was already earmarked to a specific voting snapshot tied to a destroyed address, and the code path that would compute and credit it (`VoteRewardUtil.withdrawReward` / `MortgageService.withdrawReward`) can never be invoked again for that address. This is a permanent freezing-of-funds condition reachable purely through normal, permissionless contract deployment and a `SELFDESTRUCT` call — no privileged role required.

### Likelihood Explanation
Any user can deploy a contract, have it cast votes for a super representative via the TVM vote precompile, and then call `SELFDESTRUCT` from within that contract in a later transaction (or even later in the same call context). This requires no special timing, front-run, or privileged actor — the loss occurs deterministically whenever a voting contract self-destructs while it has an outstanding, not-yet-finalized reward cycle, which is a very common scenario since a cycle typically spans a full maintenance period.

### Recommendation
Before finalizing self-destruction, force a complete settlement/withdrawal for the address (including the in-progress-cycle share, or explicitly forfeit/redirect it to the destination `obtainer` address) instead of leaving a `DelegationStore` snapshot bound to an address that can no longer trigger `withdrawReward`. Alternatively, when clearing votes in `withdrawRewardAndCancelVote`, transfer the pending `AccountVote` cycle claim (or its computed value once known) to the `obtainer` so the reward is not stranded.

### Proof of Concept
1. Enable `allowTvmVote` (already default on current mainnet parameters).
2. Deploy a contract `C` that calls the TVM vote precompile to vote for a witness `W`, funded with enough Tron Power (frozen balance) to cast votes.
3. Wait until `C`'s vote is recorded for `currentCycle` (i.e., `VoteWitnessProcessor.execute` runs, storing votes and calling `VoteRewardUtil.withdrawReward` at least once, per `VoteWitnessProcessor.execute` at `actuator/src/main/java/org/tron/core/vm/nativecontract/VoteWitnessProcessor.java` lines 39-52).
4. Before the cycle/maintenance boundary completes (i.e., before `currentCycle` increments and the reward for that vote becomes withdrawable), call `SELFDESTRUCT` from `C`, transferring its balance to some `obtainer` address.
5. Observe: `Program.suicide()` → `withdrawRewardAndCancelVote()` → `VoteRewardUtil.withdrawReward()` settles only prior completed cycles and stores a new `AccountVote` snapshot for `currentCycle` under `C`'s address, then clears votes and self-destructs `C`.
6. After the cycle completes, no transaction can ever be sent from address `C` (it no longer exists as an active/votable account) to trigger `withdrawReward` again, so the reward portion tied to that final `AccountVote(currentCycle, C)` snapshot is never paid to anyone and is effectively permanently frozen in the SR's `delegationStore` reward pool.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L553-559)
```java
    if (VMConfig.allowTvmVote()) {
      withdrawRewardAndCancelVote(owner, getContractState());
      balance = getContractState().getBalance(owner);
      if (internalTx != null && balance != internalTx.getValue()) {
        internalTx.setValue(balance);
      }
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L706-721)
```java
  private void withdrawRewardAndCancelVote(byte[] owner, Repository repo) {
    VoteRewardUtil.withdrawReward(owner, repo);

    AccountCapsule ownerCapsule = repo.getAccount(owner);
    if (!ownerCapsule.getVotesList().isEmpty()) {
      VotesCapsule votesCapsule = repo.getVotes(owner);
      if (votesCapsule == null) {
        votesCapsule = new VotesCapsule(ByteString.copyFrom(owner),
            ownerCapsule.getVotesList());
      } else {
        votesCapsule.clearNewVotes();
      }
      ownerCapsule.clearVotes();
      ownerCapsule.setOldTronPower(0);
      repo.updateVotes(owner, votesCapsule);
    }
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

**File:** chainbase/src/main/java/org/tron/core/service/MortgageService.java (L89-100)
```java
  public void withdrawReward(byte[] address) {
    if (!dynamicPropertiesStore.allowChangeDelegation()) {
      return;
    }
    AccountCapsule accountCapsule = accountStore.get(address);
    long beginCycle = delegationStore.getBeginCycle(address);
    long endCycle = delegationStore.getEndCycle(address);
    long currentCycle = dynamicPropertiesStore.getCurrentCycleNumber();
    long reward = 0;
    if (beginCycle > currentCycle || accountCapsule == null) {
      return;
    }
```
