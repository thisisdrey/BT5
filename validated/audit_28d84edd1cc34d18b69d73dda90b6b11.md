### Title
Cycle-boundary rounding lets a voter earn a full DPoS cycle's vote reward by holding a vote for a single block spanning the maintenance boundary - ([File: consensus/src/main/java/org/tron/consensus/dpos/MaintenanceManager.java])

### Summary
The Stake 2.0 / TVM vote-reward system in java-tron distributes rewards per *cycle* (the interval between two `doMaintenance()` runs) using a single, cycle-level snapshot of each witness's vote count and each voter's own vote list. Because reward accrual is granted per whole cycle rather than pro-rata to the time a vote was actually outstanding, a voter can cast a vote in the very last block of cycle `C` and immediately retract it in the very first block of cycle `C+1`, yet still be credited with the entire cycle `C+1` reward — the same "deposit at 23:59, withdraw at 00:00, still get the weekly reward" pattern described in the `LendingLedger` report, just applied to DPoS vote-cycle boundaries instead of week boundaries.

### Finding Description
Reward accrual is cycle-indexed and computed with the `Vi` (value-per-vote) accumulator pattern: [1](#0-0) 

Once per cycle, `MaintenanceManager.doMaintenance()` accumulates `Vi` for the *ending* cycle using the witness's vote count as it stood **before** the current cycle's vote-count deltas are tallied, and only afterwards applies the tallied deltas and stores them for the *next* cycle: [2](#0-1) 

This means a vote change committed during cycle `C` is not reflected in the witness's `voteCount` used for `Vi` accumulation until the maintenance that ends cycle `C` — after which the *updated* vote count (including votes cast at any point during `C`) becomes "the" vote count used for **all of** cycle `C+1`'s `Vi` accumulation, regardless of how briefly that vote is actually held during `C+1`.

On the individual-voter side, every vote-changing action (`VoteWitnessActuator.countVoteAccount`, `VoteWitnessProcessor.execute`) forces a settlement via `MortgageService.withdrawReward` / `VoteRewardUtil.withdrawReward` **before** applying the new votes: [3](#0-2) [4](#0-3) 

`withdrawReward`'s snapshot logic only checks whether `beginCycle == currentCycle`, and if a stale snapshot for that cycle does not yet exist, it falls through and re-snapshots the account's **currently active vote list** (captured a moment before the caller clears it) at `currentCycle`: [5](#0-4) 

Walking through the scenario:
1. Voter casts a vote in the last block of cycle `C` (previously had no vote). `withdrawReward` runs first with an empty vote list, so it just sets `beginCycle = C+1` and returns without a stored snapshot: [6](#0-5) 
2. Maintenance runs at the `C -> C+1` boundary. `accumulateWitnessVi(C, witness, oldVoteCount)` is computed with the pre-tally vote count (excludes the new vote for cycle `C`, correctly). The new vote's delta is then tallied into `witness.voteCount` and stored as the vote count to use for cycle `C+1`'s eventual `Vi` accumulation: [7](#0-6) 
3. In the very first block of cycle `C+1`, the voter immediately un-votes. `withdrawReward` runs again: `beginCycle == currentCycle (=C+1)` is true, but no snapshot exists yet for `C+1`, so it does **not** return early; it falls through, finds `beginCycle < endCycle` false (no reward yet, correctly zero for the still-unelapsed cycle `C+1`), and then stores a fresh snapshot `setAccountVote(C+1, address, accountCapsule)` **containing the vote that is about to be cleared** — because this snapshot is taken from the account state fetched before the actuator's `clearVotes()` call: [8](#0-7) 
4. When the `C+1 -> C+2` maintenance boundary runs, `accumulateWitnessVi(C+1, witness, voteCountAsSetAtStepStart)` uses the vote count that already includes this voter's now-retracted vote (since that count is only updated once per cycle, at the boundary, per step 2), so cycle `C+1`'s `Vi` delta is computed as if the vote was outstanding for the whole cycle.
5. On a later `withdrawReward` call, the stored snapshot for `C+1` (which still shows the vote as active) is settled against the delta `Vi` for cycle `C+1`, crediting the voter with the full cycle's reward: [9](#0-8) 

The net effect: a vote held for a single block that straddles the cycle boundary (cast just before `doMaintenance`, retracted just after) earns the same reward as a vote held for the entire cycle — structurally identical to the reported `LendingLedger` bug, where rounding reward accrual to a coarse period boundary allows disproportionate reward extraction relative to actual holding time.

### Impact Explanation
Any voter (including TVM smart-contract voters using `vote`/`withdrawreward` precompiles, or normal `VoteWitnessContract`/`WithdrawBalanceContract` senders) can repeat this "flash-vote" pattern every cycle to earn full-cycle DPoS voting rewards while keeping their TRX/tron-power available for other use during almost the entire cycle. This dilutes rewards that should accrue to voters who genuinely lock their vote for the full cycle, and lets an attacker with capital sized only for "the last block of the cycle" capture reward payouts intended for full-cycle stakers — an unbacked/disproportionate reward extraction (funds are drained from the same `Allowance`/`Vi` reward pool that honest long-term voters draw from). Because this only requires ordinary, unprivileged transactions (`VoteWitnessContract`, TVM `vote`, `withdrawreward`) timed around the known, public maintenance interval, this is a High severity issue affecting the fairness/integrity of the SR-voting incentive mechanism.

### Likelihood Explanation
The maintenance interval (`nextMaintenanceTime`) is a deterministic, publicly known on-chain value (`consensusDelegate.getNextMaintenanceTime()`), so an attacker can trivially schedule a vote transaction just before the boundary and an un-vote transaction just after it, without needing any privileged access, front-running, or SR collusion — any account with sufficient tron power can execute this via a single pair of ordinary signed transactions per cycle. Likelihood is High.

### Recommendation
Do not let a vote/un-vote transaction that crosses (or brackets) a maintenance boundary receive full-cycle credit for a vote that was outstanding for less than the cycle. Options: (1) when settling reward at `withdrawReward`, base the credited vote weight on the vote list that was actually active for the *majority* (or entirety, via time-weighting) of the cycle rather than the value in effect at the moment of the settlement call; (2) require that vote changes register as effective only for cycles fully bracketed by the change (i.e., don't allow a vote and its retraction that both occur within the immediately-affected boundary window to both count toward that cycle's snapshot); or (3) accumulate `Vi` using intra-cycle time-weighted vote counts instead of a single point-in-time snapshot per cycle, analogous to fixing `LendingLedger` by splitting rewards proportionally to time held within the period rather than rounding to the period boundary.

### Proof of Concept
Conceptual sequence (mirrors `LendingLedger`'s F/V/W diagrams already present in the repo's own tests, e.g. `testRewardAlgorithmNo1`/`No3` in `framework/src/test/java/org/tron/common/runtime/vm/VoteTest.java`):
1. At the last block before `nextMaintenanceTime` for cycle `C`, submit `VoteWitnessContract` (or TVM `vote`) with `tronPower` votes for witness `W`. `MortgageService.withdrawReward` runs first (old vote list empty), sets `beginCycle = C+1`, no snapshot stored.
2. Maintenance runs, transitioning to cycle `C+1`; `witness.voteCount` (and `delegationStore.setWitnessVote(C+1, ...)`) now includes the new vote.
3. In the very next block (still within cycle `C+1`), submit `VoteWitnessContract` with an empty vote list (or TVM un-vote) to retract the vote. `withdrawReward` stores `setAccountVote(C+1, address, accountCapsuleWithVoteStillPresent)` before the votes are cleared.
4. Advance to cycle `C+2` (via subsequent blocks/maintenance) and call `withdrawReward`/`withdrawreward()`. The settlement for cycle `C+1` uses the `Vi(C+1)` delta (computed with the voter's vote counted for the entire cycle) multiplied by the retracted vote amount, crediting the voter with the full cycle `C+1` reward despite the vote having been held for only one block.

This cannot be fully confirmed with a running Foundry-equivalent test in this session (java-tron requires building/running the JVM node and consensus loop to observe actual `Vi`/allowance values across a real maintenance boundary); the trace above is derived directly from the cited source logic. A Devin session with the ability to run the existing `VoteTest`/`DelegationServiceTest` JUnit suites (which already simulate `payRewardAndDoMaintenance` cycle-by-cycle) would be needed to execute a concrete assertion-based PoC.

### Citations

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

**File:** consensus/src/main/java/org/tron/consensus/dpos/MaintenanceManager.java (L94-162)
```java
    DynamicPropertiesStore dynamicPropertiesStore = consensusDelegate.getDynamicPropertiesStore();
    DelegationStore delegationStore = consensusDelegate.getDelegationStore();
    if (dynamicPropertiesStore.useNewRewardAlgorithm()) {
      long curCycle = dynamicPropertiesStore.getCurrentCycleNumber();
      consensusDelegate.getAllWitnesses().forEach(witness -> {
        delegationStore.accumulateWitnessVi(curCycle, witness.createDbKey(), witness.getVoteCount());
      });
    }

    Map<ByteString, Long> countWitness = countVote(votesStore);
    if (!countWitness.isEmpty()) {
      List<ByteString> currentWits = consensusDelegate.getActiveWitnesses();

      List<ByteString> newWitnessAddressList = new ArrayList<>();
      consensusDelegate.getAllWitnesses()
          .forEach(witnessCapsule -> newWitnessAddressList.add(witnessCapsule.getAddress()));

      countWitness.forEach((address, voteCount) -> {
        byte[] witnessAddress = address.toByteArray();
        WitnessCapsule witnessCapsule = consensusDelegate.getWitness(witnessAddress);
        if (witnessCapsule == null) {
          logger.warn("Witness capsule is null. address is {}", Hex.toHexString(witnessAddress));
          return;
        }
        AccountCapsule account = consensusDelegate.getAccount(witnessAddress);
        if (account == null) {
          logger.warn("Witness account is null. address is {}", Hex.toHexString(witnessAddress));
          return;
        }
        witnessCapsule.setVoteCount(witnessCapsule.getVoteCount() + voteCount);
        consensusDelegate.saveWitness(witnessCapsule);
        logger.info("address is {} , countVote is {}", witnessCapsule.createReadableString(),
            witnessCapsule.getVoteCount());
      });

      dposService.updateWitness(newWitnessAddressList);

      incentiveManager.reward(newWitnessAddressList);

      List<ByteString> newWits = consensusDelegate.getActiveWitnesses();
      if (!CollectionUtils.isEqualCollection(currentWits, newWits)) {
        currentWits.forEach(address -> {
          WitnessCapsule witnessCapsule = consensusDelegate.getWitness(address.toByteArray());
          witnessCapsule.setIsJobs(false);
          consensusDelegate.saveWitness(witnessCapsule);
        });
        newWits.forEach(address -> {
          WitnessCapsule witnessCapsule = consensusDelegate.getWitness(address.toByteArray());
          witnessCapsule.setIsJobs(true);
          consensusDelegate.saveWitness(witnessCapsule);
        });

        SRMetrics.recordSrSetChange(currentWits, newWits);
      }

      logger.info("Update witness success. \nbefore: {} \nafter: {}",
          getAddressStringList(currentWits),
          getAddressStringList(newWits));
    }

    if (dynamicPropertiesStore.allowChangeDelegation()) {
      long nextCycle = dynamicPropertiesStore.getCurrentCycleNumber() + 1;
      dynamicPropertiesStore.saveCurrentCycleNumber(nextCycle);
      consensusDelegate.getAllWitnesses().forEach(witness -> {
        delegationStore.setBrokerage(nextCycle, witness.createDbKey(),
            delegationStore.getBrokerage(witness.createDbKey()));
        delegationStore.setWitnessVote(nextCycle, witness.createDbKey(), witness.getVoteCount());
      });
    }
```

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

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/VoteWitnessProcessor.java (L39-53)
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
