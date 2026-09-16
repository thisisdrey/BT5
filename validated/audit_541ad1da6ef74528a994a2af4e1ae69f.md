### Title
Vote-reward cycle boundary allows a "flash-vote" sandwich to capture a full cycle's SR reward share while diluting long-term voters - (File: `consensus/src/main/java/org/tron/consensus/dpos/MaintenanceManager.java`)

### Summary
The Citadel finding is about a caller sandwiching a periodic reward-distribution event: deposit right before the batched mint/distribution snapshot, withdraw right after, and still receive a full share of the newly minted rewards — diluting users who were staked the whole period. java-tron's DPoS witness-vote-reward mechanism (`Vi` accumulator model) has an analogous cycle-boundary snapshot weakness: a voter who casts `VoteWitnessContract` only in the last blocks of a maintenance cycle is credited, for reward-splitting purposes, on equal footing with voters who held the vote for the entire following cycle.

### Finding Description
Reward accrual per witness uses a per-cycle accumulator `Vi` computed in `MaintenanceManager.doMaintenance()`: [1](#0-0) 

For each maintenance cycle, `delegationStore.accumulateWitnessVi(curCycle, witness, witness.getVoteCount())` is called using the witness's *current* `voteCount` snapshot, and only afterward are new votes tallied via `countVote(votesStore)` and merged into `witness.getVoteCount()` for use going forward: [2](#0-1) 

A user's individual reward share for a span of cycles is computed purely from `deltaVi` between the vote's `beginCycle` and `endCycle` multiplied by the number of votes cast, with no accounting for *when within a cycle* the vote was cast or how long it was actually held during that cycle: [3](#0-2) [4](#0-3) 

Because `witness.getVoteCount()` (and the resulting `Vi`) is only updated at maintenance boundaries, any vote broadcast at any point up to the last block before a maintenance boundary is treated identically to a vote that existed for the entire subsequent cycle when that cycle's rewards are later split. A voter therefore only needs to be "at risk" (i.e., have TRX frozen and voted) for a few blocks before a cycle boundary in order to be fully counted for the following cycle's reward distribution, exactly mirroring the Citadel bug where depositing immediately before `mintAndDistribute` captured a full share of freshly minted rewards.

### Impact Explanation
This does not create unbacked balances or let anyone steal principal, but it systematically dilutes the reward share of voters who kept their stake locked for the genuinely intended duration, in favor of an actor who only exposes capital for a small fraction of a cycle. Because this can be repeated deterministically every maintenance cycle by any account with freezable TRX, it is a reward-fairness violation reachable from an ordinary signed `VoteWitnessContract`/`FreezeBalanceV2Contract` transaction, with no special privilege required — consistent with the original finding's Medium classification (valid front-running of a periodic distribution snapshot, but not a "guaranteed high-value" exploit due to the practical constraints of un-staking delay for withdrawing the underlying principal).

### Likelihood Explanation
Exploitation only requires an unprivileged user to freeze TRX and cast (or clear) votes at chosen times relative to the publicly known, deterministic maintenance-cycle boundary (fixed interval, e.g. every 6 hours) — a normal, permissionless transaction path (`VoteWitnessActuator` / `VoteWitnessProcessor`, `WithdrawRewardProcessor`, `MortgageService.withdrawReward`). No collusion, front-running against other transactions' ordering, or privileged access is needed; timing relative to the fixed maintenance schedule is sufficient, making this readily and repeatedly executable.

### Recommendation
Weight reward accrual by the actual time (block count) a vote was outstanding within a cycle rather than by a boundary snapshot, e.g., pro-rate `Vi` contributions based on the block height at which a vote was cast/cleared within the cycle, or require a minimum holding period spanning at least one full maintenance cycle before a vote counts toward that cycle's `Vi` computation.

### Proof of Concept
1. Note the on-chain `getNextMaintenanceTime()` (deterministic, fixed interval).
2. Shortly before that boundary, freeze TRX (`FreezeBalanceV2Contract`) and cast `VoteWitnessContract` votes for a witness with a large upcoming reward pool.
3. When `MaintenanceManager.doMaintenance()` runs, `countVote` merges the new votes into `witness.getVoteCount()` (post `Vi` computation for the just-ended cycle) — see `MaintenanceManager.doMaintenance()` lines 89-127.
4. Hold the vote through the next full cycle (no need to have held it during the prior cycle).
5. After the next maintenance boundary accrues `Vi` for that cycle using the already-inflated `voteCount` that includes the flash-vote, call `WithdrawRewardProcessor.execute` / `WithdrawBalanceActuator` to redeem `deltaVi * userVote / DECIMAL_OF_VI_REWARD` — see `VoteRewardUtil.computeReward` lines 90-110 and `MortgageService.computeReward` lines 199-230 — receiving a full cycle's proportional reward despite only being exposed for a small fraction of the preceding cycle, at the expense of long-term voters' share of the same fixed reward pool.

**Note on confidence:** I was unable to fully trace `countVote(votesStore)` and `VotesStore`/`VoteWitnessActuator` details (the file read call for `VoteWitnessActuator.java` did not return content before the iteration limit), so the exact accounting of vote removals mid-cycle (e.g., whether clearing a vote before a boundary fully removes it from `witness.getVoteCount()` before that cycle's `Vi` is struck) is not fully confirmed and should be verified in a follow-up review.

### Citations

**File:** consensus/src/main/java/org/tron/consensus/dpos/MaintenanceManager.java (L89-127)
```java
  public void doMaintenance() {
    VotesStore votesStore = consensusDelegate.getVotesStore();

    tryRemoveThePowerOfTheGr();

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

**File:** chainbase/src/main/java/org/tron/core/service/MortgageService.java (L199-230)
```java
  private long computeReward(long beginCycle, long endCycle, AccountCapsule accountCapsule) {
    if (beginCycle >= endCycle) {
      return 0;
    }

    long reward = 0;
    long newAlgorithmCycle = dynamicPropertiesStore.getNewRewardAlgorithmEffectiveCycle();
    List<Pair<byte[], Long>> srAddresses = accountCapsule.getVotesList().stream()
        .map(vote -> new Pair<>(vote.getVoteAddress().toByteArray(), vote.getVoteCount()))
        .collect(Collectors.toList());
    if (beginCycle < newAlgorithmCycle) {
      long oldEndCycle = min(endCycle, newAlgorithmCycle,
          dynamicPropertiesStore.disableJavaLangMath());
      reward = getOldReward(beginCycle, oldEndCycle, srAddresses);
      beginCycle = oldEndCycle;
    }
    if (beginCycle < endCycle) {
      for (Pair<byte[], Long>  vote : srAddresses) {
        byte[] srAddress = vote.getKey();
        BigInteger beginVi = delegationStore.getWitnessVi(beginCycle - 1, srAddress);
        BigInteger endVi = delegationStore.getWitnessVi(endCycle - 1, srAddress);
        BigInteger deltaVi = endVi.subtract(beginVi);
        if (deltaVi.signum() <= 0) {
          continue;
        }
        long userVote = vote.getValue();
        reward += deltaVi.multiply(BigInteger.valueOf(userVote))
            .divide(DelegationStore.DECIMAL_OF_VI_REWARD).longValue();
      }
    }
    return reward;
  }
```
