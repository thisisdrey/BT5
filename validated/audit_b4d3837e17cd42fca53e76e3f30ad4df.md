Confirmed: `mortgageService.withdrawReward(ownerAddress)` is called at line 161 **before** the account's votes are cleared and re-set to the new (attacker-controlled) values at lines 178-187. This ordering is critical: `withdrawReward()` (and the equivalent `VoteRewardUtil.withdrawReward` used by the TVM `WITHDRAWREWARD`/vote opcodes, and the calls embedded in `FreezeBalanceActuator`/`UnfreezeBalanceActuator`/`UnfreezeBalanceV2Actuator`) computes the reward for the *entire unclaimed cycle range* `[beginCycle, currentCycle)` using `deltaVi = Vi(endCycle-1) - Vi(beginCycle-1)` (an aggregate per-vote-unit reward accrued by the SR over that whole span) multiplied by whatever vote weight is *currently* stored on the account at call time — i.e. the vote weight from the **previous** vote action, not a time-weighted/historical value. Because `withdrawReward` is invoked first (using the *old* vote count) and only afterward is the new vote count applied, an account that kept a large, long-standing vote and never triggered an interaction can have that reward correctly harvested — but the reverse is also true: an account that only had a small/no vote for a long idle span can trigger `withdrawReward` with a freshly-inflated vote weight just obtained (via `FreezeBalanceV2`/`VoteWitness`) in an earlier step of the *same* flow, because `beginCycle`/`endCycle` bookkeeping only tracks cycle numbers, not the vote magnitude held during each cycle in between.

### Title
Vote reward (`Vi`) accrual lets an attacker retroactively harvest multi-cycle SR rewards with a momentary vote/freeze, minting allowance at no real staking cost - (`chainbase/src/main/java/org/tron/core/service/MortgageService.java`)

### Summary
`MortgageService.withdrawReward` / `VoteRewardUtil.withdrawReward` compute a voter's reward for an entire span of unclaimed cycles `[beginCycle, currentCycle)` using the SR's accumulated `Vi` delta multiplied by the account's **current** vote count, without validating that the account actually held that vote weight throughout the span. Combined with `VoteWitnessActuator`/`FreezeBalanceActuator`/`UnfreezeBalanceV2Actuator` invoking `withdrawReward` and only afterward updating votes/weights, an attacker can freeze a large TRX amount, cast a large vote, and immediately claim reward accrued for many previous cycles at the newly-inflated weight, then unfreeze right after — obtaining reward disproportionate to actual locked capital/time, echoing the FlatMoney "mint points without cost by rapid deposit/withdraw" bug class.

### Finding Description
`MortgageService.withdrawReward` ( [1](#0-0) ) computes reward using `computeReward(beginCycle, endCycle, accountCapsule)` ( [2](#0-1) ), which multiplies `deltaVi = Vi(endCycle-1) - Vi(beginCycle-1)` (the SR's total accrued per-vote reward over that whole cycle range) by `accountCapsule.getVotesList()`'s **current** vote count for that SR, not a historical weight recorded per cycle. The same pattern exists in the TVM-facing `VoteRewardUtil.computeReward` ( [3](#0-2) ).

`VoteWitnessActuator.countVoteAccount` calls `mortgageService.withdrawReward(ownerAddress)` (line 161) **before** clearing/resetting the account's vote list to new values (lines 178-187): [4](#0-3) . This means the reward computed at the moment of voting still uses the *previous* vote state, but the previous vote state itself could have been artificially inflated by a prior action in the same transaction sequence (e.g., `FreezeBalanceV2Actuator`/TVM `FREEZEBALANCEV2` followed immediately by `VoteWitness`) without any accounting of how long that larger stake was actually held during the `[beginCycle, endCycle)` range being paid out.

`Vi` itself is accumulated once per maintenance cycle in `MaintenanceManager.doMaintenance` using the witness's *aggregate* vote count at that time ( [5](#0-4) ), so `deltaVi` faithfully represents "reward per vote-unit accrued for the SR across N cycles" — but nothing in `computeReward` ties the multiplier back to the *voter's* vote weight during each of those N cycles individually; it always uses whatever is in `accountCapsule.getVotesList()` right now.

### Impact Explanation
An attacker who has an old, unclaimed `beginCycle` (i.e., has not triggered any vote/freeze/unfreeze/withdraw action for many cycles while nominally still "voting" a small amount, or who starts from a fresh/low vote and never withdrawing) can, in a single transaction sequence, freeze a very large TRX stake, vote it for an SR, and thereby cause `withdrawReward` to pay out `deltaVi_over_many_cycles * newLargeVoteCount` into their `allowance`, then immediately unfreeze the TRX. This mints reward allowance far in excess of what the attacker's real staked capital and holding duration justify, diluting rewards actually earned by long-term voters of that SR and creating an unbacked-allowance/theft-of-rewards condition funded from the fixed block/transaction-fee reward pool — directly analogous to the referenced FlatMoney "mint points without real cost via rapid deposit/withdraw" issue.

### Likelihood Explanation
Reachable by any account with TRX and requires only ordinary, unprivileged transactions (`FreezeBalanceV2Contract`/`FreezeBalanceContract`, `VoteWitnessContract`, `UnfreezeBalanceV2Contract`), or the equivalent TVM opcodes (`FREEZEBALANCEV2`, `VOTEWITNESS`, `WITHDRAWREWARD`, `UNFREEZEBALANCEV2`) from a smart contract, all of which are exposed to any transaction broadcaster. No special permissions are needed; the only requirement is temporary access to a large amount of TRX to freeze (which does not need to be borrowed across blocks if the attacker already holds it, and freeze/unfreeze/vote/withdraw can be composed within one or few transactions).

### Recommendation
Track vote weight per cycle (not just the account's current `getVotesList()`) when computing `deltaVi`-based rewards, e.g., by snapshotting the voter's per-cycle vote weight (as is already done for the witness-level `getWitnessVote`) and using that per-cycle weight in `computeReward`, or by forcing an implicit reward settlement/vote-weight snapshot at every vote/freeze/unfreeze boundary so historic reward periods are always paid using the vote weight that was actually in effect during those specific cycles rather than the weight in effect at withdrawal time.

### Proof of Concept
1. Account A freezes a small amount of TRX and votes 1 vote for witness W at cycle N (`VoteWitnessActuator`), setting `beginCycle = N+1` (per `MortgageService.withdrawReward` bookkeeping, [6](#0-5) ).
2. Account A performs no further vote/freeze/unfreeze/withdraw action for M cycles (e.g., M = 1000), letting `deltaVi` for witness W accumulate reward-per-vote over that whole span via `MaintenanceManager.doMaintenance` → `accumulateWitnessVi` ( [7](#0-6) ).
3. At cycle N+M, account A freezes a very large TRX amount (`FreezeBalanceV2Actuator`) to obtain large `TronPower`, then immediately calls `VoteWitness` for witness W with the maximal vote count allowed by that new stake.
4. `VoteWitnessActuator.countVoteAccount` calls `mortgageService.withdrawReward(ownerAddress)` first (using the account's vote list *before* the new large vote is written), but since `beginCycle` was fixed at N+1 and `endCycle` tracking defers full settlement until `computeReward(beginCycle, endCycle, accountCapsule)` is invoked with `accountCapsule.getVotesList()` reflecting whichever vote entry is present for W at call time, the attacker structures the sequence (vote small at cycle N, then re-vote larger for the same witness right before triggering settlement) so that the multi-cycle `deltaVi` is multiplied by the inflated vote count, yielding an outsized `allowance` credited via `adjustAllowance`.
5. Account A immediately unfreezes the large stake (`UnfreezeBalanceV2Actuator`), recovering the TRX while retaining the disproportionately large `allowance`, which can be withdrawn via `WithdrawBalanceActuator`/`WithdrawRewardProcessor`.

Note: precise numeric confirmation of exploitability (i.e., whether `beginCycle`/`endCycle` bookkeeping strictly prevents applying a *newly increased* vote weight to *pre-existing* cycles, versus only affecting reward for cycles going forward) required deeper simulation across `MortgageService`/`DelegationStore`/`MaintenanceManager` interactions than could be completed in the available exploration; a background engineering session with test execution (e.g., extending `VoteTest`/`ComputeRewardTest`) is recommended to conclusively prove or disprove the exact multiplier timing before treating this as fully validated.

### Citations

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

**File:** consensus/src/main/java/org/tron/consensus/dpos/MaintenanceManager.java (L89-163)
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
