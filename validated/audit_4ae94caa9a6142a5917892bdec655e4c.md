### Title
Witness Vi (reward-per-vote) is finalized before standby-witness rewards for the same cycle are added, causing permanently mis-accounted voter rewards - (File: consensus/src/main/java/org/tron/consensus/dpos/MaintenanceManager.java)

### Summary
`MaintenanceManager.doMaintenance()` freezes/finalizes the per-cycle "Vi" value (reward-per-vote) used for voter reward distribution using the current, *not-yet-complete*, accumulated reward for that cycle, and only afterwards adds additional reward (standby-witness pay) into the same cycle's bucket. This is the same root-cause pattern as the Surge Pool bug: a share/ratio value is derived from an underlying quantity (`reward`, analogous to `loan_supplied`) that is still stale — additional value is credited to that same accounting period only *after* the share/ratio has already been computed and persisted.

### Finding Description
`doMaintenance()` executes, in order:

1. If `useNewRewardAlgorithm()` is enabled, it immediately finalizes and persists the per-witness Vi (reward-per-vote index) for the ending `curCycle`, based on whatever `delegationStore.getReward(curCycle, address)` currently holds: [1](#0-0) 

2. Then it tallies new votes (`countVote`) and updates witness vote counts: [2](#0-1) 

3. Only *after* Vi has already been computed and stored does it call `incentiveManager.reward(newWitnessAddressList)`, which (via `MortgageService.payStandbyWitness()` → `payReward()`) adds standby-witness (top-127) block rewards into the store using `dynamicPropertiesStore.getCurrentCycleNumber()`: [3](#0-2) [4](#0-3) 

4. The current cycle number is only incremented afterwards, at the very end of `doMaintenance()`: [5](#0-4) 

Because the cycle number used by `payReward()` (step 3) is not incremented until step 4, the standby-witness reward paid in step 3 is credited to the *same* `curCycle` whose Vi was already permanently written to `rewardViStore`/`DelegationStore` in step 1: [6](#0-5) 

`accumulateWitnessVi` computes `deltaVi = reward * DECIMAL_OF_VI_REWARD / voteCount` and adds it to the previous cycle's Vi to get the new, permanent per-cycle Vi: [7](#0-6) 

Voter rewards are later computed strictly from the delta between two cycles' stored Vi values (`endVi - beginVi`), multiplied by the voter's vote count: [8](#0-7) 

Once Vi for `curCycle` is written, it is never recomputed, so any reward subsequently added to `delegationStore` for that same cycle (the standby-witness pay from step 3) is excluded from the Vi delta that voters use to claim their share of rewards — this is structurally identical to the Surge Pool issue, where `fee_share` is computed from `supplied` before `interest` (a value credited within the same operation) is folded in.

### Impact Explanation
The standby-witness reward portion added in step 3 for `curCycle` is silently excluded from that cycle's Vi computation, meaning voters who voted for those standby (non-top-27) witnesses never receive their proportional share of that reward through the Vi-based `withdrawReward`/`queryReward` mechanism, even though `delegationStore.getReward(curCycle, address)` (or the underlying balance) was credited with it. This results in a permanent, unbacked discrepancy between reward accounted for in storage and reward actually distributable to voters through the Vi mechanism — a form of fund mis-accounting/loss for voters reachable purely through normal chain operation (voting + block production), without requiring any privileged or malicious actor.

### Likelihood Explanation
This triggers on every maintenance cycle boundary when `useNewRewardAlgorithm()` is active and any standby witness (paid via `payStandbyWitness`) is voted for — i.e., under completely ordinary, non-adversarial network operation on every maintenance cycle, since `doMaintenance()` runs at every cycle rollover, `useNewRewardAlgorithm` gates the new Vi mechanism, and `incentiveManager.reward()` is unconditionally invoked afterward.

### Recommendation
Reorder `doMaintenance()` so that all reward crediting for `curCycle` (including `incentiveManager.reward(...)`, which triggers `payStandbyWitness`) completes *before* `accumulateWitnessVi` reads `delegationStore.getReward(curCycle, address)` and finalizes each witness's Vi for that cycle. Alternatively, ensure `payReward()` always attributes rewards to the correct, already-closed cycle rather than relying on `dynamicPropertiesStore.getCurrentCycleNumber()`, which is only incremented later in the same function.

### Proof of Concept
Conceptual trace through `MaintenanceManager.doMaintenance()` with `useNewRewardAlgorithm() == true`:
1. `curCycle = 100` (not yet incremented).
2. Line 96-101 runs: for each witness (including a standby witness `W`), `delegationStore.accumulateWitnessVi(100, W, oldVoteCount)` reads `delegationStore.getReward(100, W)` = R (already includes block/tx-fee rewards accrued during cycle 100 via `payBlockReward`/`payTransactionFeeReward`), computes `deltaVi = R * DECIMAL / oldVoteCount`, and permanently stores `Vi(100, W) = Vi(99, W) + deltaVi`.
3. Line 131 runs: `incentiveManager.reward(...)` → `payStandbyWitness()` computes `pay` for `W` and calls `payReward(W, pay)`, which (still `getCurrentCycleNumber() == 100`) calls `delegationStore.addReward(100, W, pay)` — increasing `delegationStore.getReward(100, W)` to `R + pay`.
4. Lines 154-162 run: `currentCycleNumber` is incremented to 101, and `witnessVote(101, W)` is set.
5. Any voter for `W` who later withdraws reward computes it strictly from `Vi(100,W) - Vi(begin,W)` (per `computeReward` in `MortgageService`/`VoteRewardUtil`), which never reflects the `pay` amount added in step 3 — that portion of the reward is stranded/unaccounted for in the Vi-based payout path even though it was added to `delegationStore`'s reward bucket for cycle 100. [9](#0-8)

### Citations

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

**File:** chainbase/src/main/java/org/tron/core/service/MortgageService.java (L53-87)
```java
  public void payStandbyWitness() {
    List<WitnessCapsule> witnessStandbys = witnessStore.getWitnessStandby(
        dynamicPropertiesStore.allowWitnessSortOptimization());
    long voteSum = witnessStandbys.stream().mapToLong(WitnessCapsule::getVoteCount).sum();
    if (voteSum < 1) {
      return;
    }
    long totalPay = dynamicPropertiesStore.getWitness127PayPerBlock();
    double eachVotePay = (double) totalPay / voteSum;
    for (WitnessCapsule w : witnessStandbys) {
      long pay = (long) (w.getVoteCount() * eachVotePay);
      payReward(w.getAddress().toByteArray(), pay);
      logger.debug("Pay {} stand reward {}.", Hex.toHexString(w.getAddress().toByteArray()), pay);
    }
  }

  public void payBlockReward(byte[] witnessAddress, long value) {
    logger.debug("Pay {} block reward {}.", Hex.toHexString(witnessAddress), value);
    payReward(witnessAddress, value);
  }

  public void payTransactionFeeReward(byte[] witnessAddress, long value) {
    logger.debug("Pay {} transaction fee reward {}.", Hex.toHexString(witnessAddress), value);
    payReward(witnessAddress, value);
  }

  private void payReward(byte[] witnessAddress, long value) {
    long cycle = dynamicPropertiesStore.getCurrentCycleNumber();
    int brokerage = delegationStore.getBrokerage(cycle, witnessAddress);
    double brokerageRate = (double) brokerage / 100;
    long brokerageAmount = (long) (brokerageRate * value);
    value -= brokerageAmount;
    delegationStore.addReward(cycle, witnessAddress, value);
    adjustAllowance(witnessAddress, brokerageAmount);
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

**File:** chainbase/src/main/java/org/tron/core/service/RewardViCalService.java (L215-229)
```java
  private void accumulateWitnessVi(long cycle, byte[] address) {
    BigInteger preVi = getWitnessVi(cycle - 1, address);
    long voteCount = getWitnessVote(cycle, address);
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
