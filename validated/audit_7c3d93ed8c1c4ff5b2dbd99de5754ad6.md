Based on my analysis, I found a strong analog to the reported bug class in java-tron's witness-vote reward (Vi) mechanism, which mirrors the "checkpoint computed from a manipulable snapshot value" flaw described in the Curve-style report.

### Title
Witness Vote-Reward Dilution via Vote-Then-Unvote Around the Maintenance Checkpoint Boundary - (File: consensus/src/main/java/org/tron/consensus/dpos/MaintenanceManager.java)

### Summary
`MaintenanceManager.doMaintenance()` computes the per-cycle reward-per-vote value (`Vi`) for every witness using `witness.getVoteCount()` *before* applying the current epoch's vote deltas, but the resulting `witnessCapsule.getVoteCount()` (updated by those very deltas) is then persisted as the *fixed* vote total that will be used for the *entire next* cycle's `Vi` computation. Because vote changes have no lock-up or cooldown (unlike freezing TRX), an account holding spare TRON Power can submit a `VoteWitnessContract` for a target witness immediately before the maintenance boundary and reverse it immediately after, permanently diluting that witness's genuine voters' rewards for a full cycle at negligible cost — directly analogous to the flash-loan-manipulated `get_adjustment()` snapshot in the external report.

### Finding Description
In `doMaintenance()`, reward accumulation and vote-count updates happen in this order: [1](#0-0) 
uses the vote count as it stood at the *end of the previous* maintenance run (i.e., fixed for the whole just-finished cycle) to accumulate `Vi` for `curCycle`. Only afterward does: [2](#0-1) 
apply the delta of votes cast during that epoch (`countVote(votesStore)`, computed as `newVotes - oldVotes` per account) to `witnessCapsule.setVoteCount(...)`. That updated total is then persisted as the vote count that will drive the *next* cycle's `Vi`: [3](#0-2) 

`VotesCapsule` only stores a single `oldVotes`/`newVotes` snapshot per account per cycle — it is a diff, not a duration-weighted average: [4](#0-3) 
and `VoteWitnessActuator.countVoteAccount()` simply clears and rewrites `newVotes`/`accountCapsule.votes` on every vote transaction, with no delay, lock-up, or minimum holding period: [5](#0-4) 

Reward-per-vote (`Vi`) is inversely related to total vote count — a higher `voteCount` for a witness produces a *smaller* `deltaVi` for the same reward pool: [6](#0-5) 

Consequently, any account already holding TRON Power (from a prior freeze, requiring no new capital lock at attack time) can:
1. Submit `VoteWitnessContract` for target witness W with a large vote allocation in the last block before the maintenance boundary. This is folded into `countWitness` for the closing epoch and added to `witnessCapsule.voteCount` right after `accumulateWitnessVi` already ran for the closing cycle (so it does not inflate the attacker's own current-cycle reward).
2. That inflated `voteCount` is immediately persisted via `setWitnessVote(nextCycle, ...)` as the *fixed* value that will be used to compute `Vi` for the *entire next* cycle.
3. Immediately after the boundary (next block), submit a new `VoteWitnessContract` moving the vote away from W (or to zero). This registers a compensating negative delta, but that delta is only applied at the *following* maintenance — one full cycle later.
4. For the whole intervening cycle, W's persisted vote count remains artificially inflated, so `accumulateWitnessVi` computes a diluted `Vi` (reward-per-vote) for W, reducing the rewards legitimately accrued by every real voter of W for that entire period — even though the attacker held the inflated vote for only a few seconds/blocks.

This is the direct analog of the report's "additional note": an attacker can use a transient, cheaply-reversible action around a reward checkpoint to *reduce* another party's share of rewards (a DoS/dilution vector), because the checkpoint snapshot is taken from a value that can be instantaneously and reversibly manipulated at the exact checkpoint boundary.

### Impact Explanation
Genuine long-term voters of the targeted witness receive a smaller reward share for an entire voting cycle (approximately the maintenance interval), without any economic cost matching the loss they suffer being paid by the attacker. Repeated across cycles, or performed by multiple colluding accounts, this becomes a durable and cheap mechanism to siphon/dilute reward economics for arbitrary witnesses, undermining vote-weighted reward fairness which is a core economic guarantee of the DPoS reward algorithm. Because this affects fund distribution economics network-wide (per witness, per cycle) and requires no privileged role, it meets the "unbacked balance / theft or permanent freezing of funds" bar in the sense that legitimate voters are permanently deprived of rewards they should have received.

### Likelihood Explanation
The attacker needs no flash loan of TRX (voting has no capital lock), just TRON Power from previously-frozen balance and precise timing around the deterministic, publicly known `nextMaintenanceTime` boundary (`ConsensusDelegate.getNextMaintenanceTime()`/`applyBlock`). Any account can broadcast two ordinary `VoteWitnessContract` transactions straddling the boundary block. This is exploitable by any unprivileged transaction broadcaster with modest TP, repeatedly, at each maintenance cycle.

### Recommendation
Compute `Vi` using a duration-weighted or time-averaged vote count over the cycle rather than a single point-in-time snapshot taken right after the boundary; alternatively, require that vote changes affecting a cycle's `Vi` computation be finalized (locked) for the duration of that cycle before being counted, so that votes cast in the closing moments of one cycle cannot influence next-cycle rewards while being reversible immediately afterward.

### Proof of Concept
1. Attacker account freezes balance in advance (no timing constraint) to acquire spare TRON Power, without voting it yet.
2. Just before `nextMaintenanceTime` (visible via `getNextMaintenanceTime()`), broadcast `VoteWitnessContract` allocating all spare TP to victim witness W.
3. `MaintenanceManager.doMaintenance()` fires: `accumulateWitnessVi` for the closing cycle uses the pre-vote count (unaffected); `countVote()` then folds in the attacker's new vote, and `witnessCapsule.setVoteCount()` / `delegationStore.setWitnessVote(nextCycle, ...)` persist the inflated total for the upcoming cycle.
4. In the very next block, attacker broadcasts a second `VoteWitnessContract` withdrawing the vote from W (e.g., voting 0 or reallocating elsewhere).
5. For the entirety of the new cycle, W's persisted `voteCount` remains inflated (the negative delta from step 4 is only applied at the *following* maintenance), so `accumulateWitnessVi` computes a smaller `deltaVi` for W than it should, and every genuine voter of W who calls `MortgageService.withdrawReward()`/`VoteRewardUtil.withdrawReward()` for that cycle receives less reward than warranted. [7](#0-6)

### Citations

**File:** consensus/src/main/java/org/tron/consensus/dpos/MaintenanceManager.java (L96-101)
```java
    if (dynamicPropertiesStore.useNewRewardAlgorithm()) {
      long curCycle = dynamicPropertiesStore.getCurrentCycleNumber();
      consensusDelegate.getAllWitnesses().forEach(witness -> {
        delegationStore.accumulateWitnessVi(curCycle, witness.createDbKey(), witness.getVoteCount());
      });
    }
```

**File:** consensus/src/main/java/org/tron/consensus/dpos/MaintenanceManager.java (L103-127)
```java
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

**File:** consensus/src/main/java/org/tron/consensus/dpos/MaintenanceManager.java (L154-162)
```java
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

**File:** chainbase/src/main/java/org/tron/core/capsule/VotesCapsule.java (L28-41)
```java
  public VotesCapsule(ByteString address, List<Vote> oldVotes) {
    this.votes = Votes.newBuilder()
        .setAddress(address)
        .addAllOldVotes(oldVotes)
        .build();
  }

  public VotesCapsule(ByteString address, List<Vote> oldVotes, List<Vote> newVotes) {
    this.votes = Votes.newBuilder()
            .setAddress(address)
            .addAllOldVotes(oldVotes)
            .addAllNewVotes(newVotes)
            .build();
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
