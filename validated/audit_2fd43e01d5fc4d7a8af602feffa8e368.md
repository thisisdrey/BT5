### Title
Last-block vote sniping lets stakers farm a full cycle's SR voting reward via Vi accumulator without holding votes for the cycle - (File: chainbase/src/main/java/org/tron/core/service/MortgageService.java)

### Summary
The reported Autonolas issue is that `liquidity_lockbox` mints LP-share tokens for a deposit and lets the depositor immediately withdraw, capturing already-accrued LP rewards without ever bearing the risk/duration that earned them. Java-tron's witness-vote reward system (`VoteWitnessContract` / `MortgageService` / `VoteRewardUtil`) has the same structural flaw: rewards are accrued per maintenance cycle using a Vi ("value-per-vote") accumulator computed **once at cycle boundary** from the *final* vote tally of that cycle, and a voter's `beginCycle` is set to the *current* (in-progress) cycle as soon as they cast a vote — with **no lock-up** on voting/un-voting.

### Finding Description
- `VoteWitnessActuator.countVoteAccount()` / `VoteWitnessProcessor.execute()` call `mortgageService.withdrawReward(ownerAddress)` (or `VoteRewardUtil.withdrawReward`) and then set `beginCycle = currentCycle` for the new vote, with no freeze/lock requirement on the vote itself: [1](#0-0) [2](#0-1) 
- At every maintenance (cycle) boundary, `MaintenanceManager.doMaintenance()` accumulates the witness Vi accumulator using the witness vote count *after* all votes cast during that cycle (including a vote cast in the last block before maintenance) have been applied: [3](#0-2) [4](#0-3) 
- When the voter next touches their reward (e.g. via a vote change, `UnfreezeBalanceV2`, or `withdrawReward`), the "latest cycle" branch of `computeReward`/`withdrawReward` pays out `deltaVi(beginCycle-1, endCycle-1) * userVoteCount`, i.e., the **entire** cycle's Vi delta applied to the voter's full vote count, regardless of how many blocks within that cycle the vote was actually held: [5](#0-4) [6](#0-5) 

Because `VoteWitnessContract` has no cooldown or minimum holding duration (unlike `FreezeBalanceContract`/`FreezeBalanceV2Contract`, which impose multi-day unstaking locks and are the actual capital-lockup mechanism preventing an LP-lockbox-style flash deposit/withdraw), an account that already holds TronPower (from previously frozen TRX) can:
1. Vote a large amount of TronPower for a witness in the last block(s) before the cycle's maintenance triggers.
2. Let maintenance run, which bakes the just-arrived vote into that cycle's final `witnessVote` count used to compute the cycle's Vi delta.
3. On the very next interaction (vote-change, `UnfreezeBalanceV2`, or explicit reward query/withdraw), receive the full cycle's proportional reward as though the vote had been held for the entire cycle.
4. Immediately clear/change the vote (no lock) and repeat next cycle, or against a different witness.

### Impact Explanation
This lets a well-timed voter siphon a disproportionate share of an SR's per-cycle reward pool while diluting the totalVote denominator only at the very last moment, unfairly reducing the effective per-vote reward paid to voters who genuinely held their vote through the whole cycle. Repeated across cycles this is a systematic, no-cost transfer of value from long-term voters/witness reward pools to the sniper, i.e. an unbacked/unfair extraction of protocol reward funds — the same "theft of yield without bearing the underlying duration/risk" class as the referenced LP-lockbox report.

### Likelihood Explanation
Reachable by any account holding TronPower via a normal `VoteWitnessContract`/`VoteWitnessProcessor` (TVM) call, requiring only knowledge of the maintenance cycle boundary (which is a public, predictable on-chain timestamp `getNextMaintenanceTime`). No special privilege, freeze, or cooldown blocks this sequence, making it practically exploitable by any unprivileged transaction broadcaster.

### Recommendation
Prorate voting rewards by the fraction of the cycle during which the vote was actually held (e.g., track vote-weighted time within a cycle, or snapshot vote counts at the *start* of the cycle rather than crediting full-cycle Vi delta to votes cast at the very end), and/or introduce a minimum holding period before a newly cast vote becomes eligible for that cycle's reward.

### Proof of Concept
1. Attacker already holds frozen TRX (TronPower) from prior staking (no new freeze/lock needed).
2. Just before `nextMaintenanceTime`, attacker calls `VoteWitnessContract` (or the TVM `vote` native contract) to vote all their TronPower for witness `W`. This sets their `beginCycle = currentCycle (N)` per `MortgageService.withdrawReward`/`VoteRewardUtil.withdrawReward`.
3. `MaintenanceManager.doMaintenance()` runs, calling `delegationStore.accumulateWitnessVi(N, W, witness.getVoteCount())` where `witness.getVoteCount()` already includes the attacker's just-cast vote, and cycle advances to `N+1`.
4. Attacker immediately calls another vote-change (or `UnfreezeBalanceV2`, or queries via `withdrawReward`), triggering the "latest cycle" branch in `VoteRewardUtil.withdrawReward`/`MortgageService.withdrawReward`, which pays `deltaVi(N-1,N) * fullVoteCount` — the full cycle's reward — despite the vote having been active for only the final block(s) of cycle N.
5. Attacker clears the vote (no penalty) and repeats next cycle, extracting reward disproportionate to actual time-at-risk each cycle.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/VoteWitnessActuator.java (L152-191)
```java
  private void countVoteAccount(VoteWitnessContract voteContract) {
    AccountStore accountStore = chainBaseManager.getAccountStore();
    VotesStore votesStore = chainBaseManager.getVotesStore();
    MortgageService mortgageService = chainBaseManager.getMortgageService();
    byte[] ownerAddress = voteContract.getOwnerAddress().toByteArray();

    VotesCapsule votesCapsule;

    //
    mortgageService.withdrawReward(ownerAddress);

    AccountCapsule accountCapsule = accountStore.get(ownerAddress);

    DynamicPropertiesStore dynamicStore = chainBaseManager.getDynamicPropertiesStore();
    if (dynamicStore.supportAllowNewResourceModel()
        && accountCapsule.oldTronPowerIsNotInitialized()) {
      accountCapsule.initializeOldTronPower();
    }

    if (!votesStore.has(ownerAddress)) {
      votesCapsule = new VotesCapsule(voteContract.getOwnerAddress(),
          accountCapsule.getVotesList());
    } else {
      votesCapsule = votesStore.get(ownerAddress);
    }

    accountCapsule.clearVotes();
    votesCapsule.clearNewVotes();

    voteContract.getVotesList().forEach(vote -> {
      logger.debug("countVoteAccount, address[{}]",
          ByteArray.toHexString(vote.getVoteAddress().toByteArray()));

      votesCapsule.addNewVotes(vote.getVoteAddress(), vote.getVoteCount());
      accountCapsule.addVotes(vote.getVoteAddress(), vote.getVoteCount());
    });

    accountStore.put(accountCapsule.createDbKey(), accountCapsule);
    votesStore.put(ownerAddress, votesCapsule);
  }
```

**File:** chainbase/src/main/java/org/tron/core/service/MortgageService.java (L89-134)
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
    if (beginCycle == currentCycle) {
      AccountCapsule account = delegationStore.getAccountVote(beginCycle, address);
      if (account != null) {
        return;
      }
    }
    //withdraw the latest cycle reward
    if (beginCycle + 1 == endCycle && beginCycle < currentCycle) {
      AccountCapsule account = delegationStore.getAccountVote(beginCycle, address);
      if (account != null) {
        reward = computeReward(beginCycle, endCycle, account);
        adjustAllowance(address, reward);
        reward = 0;
        logger.info("Latest cycle reward {}, {}.", beginCycle, account.getVotesList());
      }
      beginCycle += 1;
    }
    //
    endCycle = currentCycle;
    if (CollectionUtils.isEmpty(accountCapsule.getVotesList())) {
      delegationStore.setBeginCycle(address, endCycle + 1);
      return;
    }
    if (beginCycle < endCycle) {
      reward += computeReward(beginCycle, endCycle, accountCapsule);
      adjustAllowance(address, reward);
    }
    delegationStore.setBeginCycle(address, endCycle);
    delegationStore.setEndCycle(address, endCycle + 1);
    delegationStore.setAccountVote(endCycle, address, accountCapsule);
    logger.info("Adjust {} allowance {}, now currentCycle {}, beginCycle {}, endCycle {}, "
            + "account vote {}.", Hex.toHexString(address), reward, currentCycle,
        beginCycle, endCycle, accountCapsule.getVotesList());
  }
```

**File:** consensus/src/main/java/org/tron/consensus/dpos/MaintenanceManager.java (L94-101)
```java
    DynamicPropertiesStore dynamicPropertiesStore = consensusDelegate.getDynamicPropertiesStore();
    DelegationStore delegationStore = consensusDelegate.getDelegationStore();
    if (dynamicPropertiesStore.useNewRewardAlgorithm()) {
      long curCycle = dynamicPropertiesStore.getCurrentCycleNumber();
      consensusDelegate.getAllWitnesses().forEach(witness -> {
        delegationStore.accumulateWitnessVi(curCycle, witness.createDbKey(), witness.getVoteCount());
      });
    }
```

**File:** chainbase/src/main/java/org/tron/core/store/DelegationStore.java (L133-146)
```java
  public void accumulateWitnessVi(long cycle, byte[] address, long voteCount) {
    BigInteger preVi = getWitnessVi(cycle - 1, address);
    long reward = getReward(cycle, address);
    if (reward == 0 || voteCount == 0) { // Just forward pre vi
      if (!BigInteger.ZERO.equals(preVi)) { // Zero vi will not be record
        setWitnessVi(cycle, address, preVi);
      }
    } else { // Accumulate delta vi
      BigInteger deltaVi = BigInteger.valueOf(reward)
          .multiply(DECIMAL_OF_VI_REWARD)
          .divide(BigInteger.valueOf(voteCount));
      setWitnessVi(cycle, address, preVi.add(deltaVi));
    }
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

**File:** actuator/src/main/java/org/tron/core/vm/utils/VoteRewardUtil.java (L90-110)
```java
  private static long computeReward(long beginCycle, long endCycle,
                                    AccountCapsule accountCapsule, Repository repository) {
    if (beginCycle >= endCycle) {
      return 0;
    }

    long reward = 0;
    for (Protocol.Vote vote : accountCapsule.getVotesList()) {
      byte[] srAddress = vote.getVoteAddress().toByteArray();
      BigInteger beginVi = repository.getDelegationStore().getWitnessVi(beginCycle - 1, srAddress);
      BigInteger endVi = repository.getDelegationStore().getWitnessVi(endCycle - 1, srAddress);
      BigInteger deltaVi = endVi.subtract(beginVi);
      if (deltaVi.signum() <= 0) {
        continue;
      }
      long userVote = vote.getVoteCount();
      reward += deltaVi.multiply(BigInteger.valueOf(userVote))
          .divide(DelegationStore.DECIMAL_OF_VI_REWARD).longValue();
    }
    return reward;
  }
```
