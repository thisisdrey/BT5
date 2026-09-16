## Title
Witness block/transaction-fee rewards are permanently lost when a witness's vote count is zero at cycle-close accumulation - (File: `chainbase/src/main/java/org/tron/core/store/DelegationStore.java`)

## Summary
The Sherlock report describes premium that is credited to a bookkeeping variable but never propagated into the value that is actually paid out, because a comparison condition (`_latestPaymentTimestamp < _startTimestamp`) silently skips accrual instead of still crediting the amount owed. Java-tron's DPoS reward-distribution pipeline has an analogous pattern: block/standby/transaction-fee rewards are unconditionally recorded per cycle in `DelegationStore`, but the per-cycle "Vi" (value-per-vote index) used to actually let voters claim that reward is updated by `accumulateWitnessVi`, which silently discards the recorded reward whenever the witness's stored vote count is `0` at that moment.

## Finding Description
Every block reward and standby-witness reward is unconditionally added to the delegation store regardless of the witness's current vote count: [1](#0-0) 

This raw per-cycle reward total is only ever consumed inside `accumulateWitnessVi`, which converts the flat reward amount into a delta applied to the running "Vi" index that voters use to compute their claimable reward: [2](#0-1) 

If `voteCount == 0` at the time this function runs for a given cycle/witness, the branch simply forwards the previous Vi unchanged — the `reward` value fetched from `getReward(cycle, address)` (which can be non-zero, e.g. from block/standby-witness payouts already credited to that witness for that cycle) is discarded and never folded into the Vi index. This exact code path (and its duplicate in `RewardViCalService`) is invoked from the consensus maintenance loop for every witness, every cycle: [3](#0-2) [4](#0-3) 

Since `computeReward`/`queryReward`/`withdrawReward` in `MortgageService` and `VoteRewardUtil` only ever compute a voter's payout via `deltaVi = endVi - beginVi`, any reward whose cycle's Vi update was skipped this way becomes permanently unclaimable by any of the witness's voters — the amount stays recorded in the `delegation` store's per-cycle reward key but is never reflected in the Vi ledger that voters read: [5](#0-4) 

This mirrors the Carapace bug class precisely: a value is credited/accounted for (`totalPremium` there, `delegationStore.addReward` here) but a subsequent condition (`_latestPaymentTimestamp < _startTimestamp` there, `voteCount == 0` here) prevents it from ever being folded into the accrual mechanism that actually releases it to the beneficiary, with no later "expiration"/catch-up path to reclaim it.

The vote-count field consumed by `accumulateWitnessVi` at maintenance time is the witness's vote tally as of the *previous* cycle's tally update (vote counting for the new cycle happens later in the same `doMaintenance()` call, see lines 103-127 in `MaintenanceManager.java`, after line 99). Consequently, any witness whose voters fully unvote/clear votes in one cycle (an ordinary `VoteWitnessContract`/`UnfreezeBalanceV2` operation any account can broadcast) while the witness is still active and earning block/standby rewards in that same cycle will reach `voteCount == 0` at the moment `accumulateWitnessVi` runs, causing that cycle's entire reward for the witness to be dropped.

## Impact Explanation
Reward funds legitimately earned by a Super Representative for block production or standing in the top-127 (paid via `payBlockReward`/`payTransactionFeeReward`/`payStandbyWitness`) become permanently unclaimable by that witness's voters whenever the witness's tracked vote count is zero at cycle boundary. This is a permanent freezing/loss of funds for the voters who are entitled to a share of the reward, reachable purely through normal vote/unvote transactions that any account can broadcast — no privileged role is required.

## Likelihood Explanation
This requires a witness's tracked vote count to reach exactly zero at the specific point `accumulateWitnessVi` executes during maintenance, while the witness still had activity-based reward credited for that cycle. This is a real, if narrow, timing condition achievable by ordinary unvoting transactions around cycle boundaries (e.g., a witness losing all votes just before/at cycle-close while still receiving block rewards from the prior period), making it plausible but not trivially guaranteed on every cycle.

## Recommendation
When `voteCount == 0` but `reward > 0` in `accumulateWitnessVi` (`DelegationStore.java` and its duplicate in `RewardViCalService.java`), do not silently discard the reward. Either roll it forward to be distributed once votes resume, redirect it to the witness's own allowance (since there is no voter base to distribute to), or otherwise ensure the previously-credited `delegationStore.addReward` amount is not permanently orphaned in a per-cycle key that no other code path ever reads.

## Proof of Concept
1. Witness `W` is an active SR and produces blocks; `payBlockReward`/`payTransactionFeeReward` credits `delegationStore.addReward(cycle, W, value)` for the current cycle (`MortgageService.java:69-87`).
2. Before the cycle's maintenance runs, all voters of `W` unvote / clear their votes via ordinary `VoteWitnessContract` transactions, driving `W`'s stored `voteCount` to `0` by the time `doMaintenance()` executes `accumulateWitnessVi(curCycle, W, 0)` (`MaintenanceManager.java:96-101`).
3. Inside `accumulateWitnessVi` (`DelegationStore.java:133-146`), since `voteCount == 0`, the branch simply forwards the previous Vi and the non-zero `reward` fetched via `getReward(cycle, W)` is never applied.
4. No voter of `W` can ever recover this reward: `computeReward`/`queryReward` only read `deltaVi` between recorded Vi snapshots, and the cycle's raw reward entry in the `delegation` store is never consumed again — the funds are permanently locked/lost, analogous to the reported Carapace premium never being accrued.

### Citations

**File:** chainbase/src/main/java/org/tron/core/service/MortgageService.java (L79-87)
```java
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

**File:** consensus/src/main/java/org/tron/consensus/dpos/MaintenanceManager.java (L96-101)
```java
    if (dynamicPropertiesStore.useNewRewardAlgorithm()) {
      long curCycle = dynamicPropertiesStore.getCurrentCycleNumber();
      consensusDelegate.getAllWitnesses().forEach(witness -> {
        delegationStore.accumulateWitnessVi(curCycle, witness.createDbKey(), witness.getVoteCount());
      });
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
