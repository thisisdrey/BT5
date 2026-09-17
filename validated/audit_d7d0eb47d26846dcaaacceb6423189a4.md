## Analysis

The reported bug class — actors timing deposits/withdrawals around discrete reward-snapshot events to capture profits they did not economically earn — maps directly onto java-tron's cycle-based Super Representative (SR) voting reward mechanism (the "new reward algorithm" / `Vi` accumulator model), which is functionally the same "share-price accumulator updated at fixed epochs" pattern as River's `BeaconValidatorBalanceSum`.

### Root cause

Reward accrual for SR voters is computed once per maintenance cycle. At every maintenance run, `MaintenanceManager.doMaintenance` accumulates a global per-witness reward index (`Vi`) for the entire cycle that just elapsed: [1](#0-0) 

The per-witness `Vi` delta reflects the *whole cycle's* accumulated block/transaction-fee reward for that witness: [2](#0-1) 

When a voter later withdraws/claims, their reward for a cycle is computed as `deltaVi * currentVoteCount`, with no accounting for *when within the cycle* the vote was actually cast: [3](#0-2) 

Crucially, `VoteWitnessActuator.countVoteAccount` lets any account change its vote allocation at any block, and the new vote count immediately becomes the "vote count for the current (in‑progress) cycle" once `mortgageService.withdrawReward` snapshots the account's prior state and resets `beginCycle` to the *current* cycle: [4](#0-3) [5](#0-4) 

There is no time-weighting: a voter who casts a vote one block before the cycle boundary (maintenance trigger) is treated identically, at settlement, to a voter who held that vote for the entire cycle (which can be hours), because `computeReward`/`queryReward` only look at `beginCycle`→`endCycle` deltaVi and the account's *current* vote count, not duration-within-cycle.

### PoC outline

1. Monitor a target SR's expected block/transaction-fee income for the current maintenance cycle (public chain data — block rewards and vote counts are all on-chain and predictable near cycle end).
2. Just before the maintenance timestamp (`doMaintenance` trigger, `dynamicPropertiesStore.getNextMaintenanceTime()`), submit a `VoteWitnessContract` transaction allocating a large amount of TRX-derived votes to that SR.
3. Once `doMaintenance` runs, `accumulateWitnessVi` credits a full cycle's `deltaVi` based on the witness's total end-of-cycle vote count (which now includes the attacker's last-second votes), and the attacker's `beginCycle`/`endCycle` window spans that just-elapsed cycle.
4. In the immediately following cycle, the attacker calls `WithdrawBalanceContract`/vote-change to withdraw the accrued `allowance` via `MortgageService.withdrawReward` → `adjustAllowance`, and can simultaneously re-vote elsewhere or unvote, exiting the position with reward earned for a cycle they only held for seconds.
5. This dilutes the reward pool share of long-term voters for that witness, since `voteRate = userVote/totalVote` includes the attacker's transient votes in `totalVote` used for that cycle's `Vi` delta, but the attacker contributed to the SR's block-production/security posture for effectively zero time.

### Impact

Repeatable across every maintenance cycle (6 hours by default, `MAINTENANCE_TIME_INTERVAL` dynamic parameter), an attacker with sufficient TRX can systematically extract a disproportionate share of SR voting rewards, unjustly diluting genuine long-term voters — an unauthorized redistribution of reward funds, which is the same "harvest profits made by others" pattern described in the source report.

### Recommendation

Time-weight the vote-count contribution used in `Vi` accumulation and in `computeReward`, e.g., by tracking a per-cycle stake-time-weighted vote count, or by only counting votes that were in place at the *start* of the cycle (snapshot vote counts at cycle start rather than cycle end) so that changing votes mid-cycle cannot retroactively earn a full cycle's reward.

Given this is an information/explanation request about analyzing a known bug class against the java-tron reward code, no code changes are being made here, so a task plan is not applicable unless you want a background agent to implement the fix.

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
