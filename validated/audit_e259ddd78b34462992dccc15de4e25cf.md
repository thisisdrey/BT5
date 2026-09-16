### Title
Block/Transaction Fee Rewards Are Permanently Lost for Cycles Where a Witness Received Zero Votes - ([File: chainbase/src/main/java/org/tron/core/store/DelegationStore.java])

### Summary
`MortgageService.payReward()` credits block/transaction-fee rewards into `delegationStore` for the *current cycle* of the witness that produced the block, independent of whether that witness had any votes/voters in that cycle. When the maintenance cycle later runs, `DelegationStore.accumulateWitnessVi()` (invoked from `MaintenanceManager.doMaintenance()`) is responsible for turning the per-cycle reward into a `Vi` (reward-per-vote) accumulator that voters use to claim their share via `MortgageService.computeReward()`. If the witness's vote count for that cycle is `0`, `accumulateWitnessVi()` skips the accumulation step entirely, and the previously stored reward for that cycle is never folded into `Vi`. The reward amount stays recorded under a `reward` key in `delegationStore` but is never referenced or added by any future computation, so it is permanently unclaimable by any account — an exact analog of the reported `rewardPerToken()` totalSupply==0 issue in the `StakingRewards` contract, where rewards keep accruing but cannot be distributed because there are no participants to receive them.

### Finding Description
The reward/Vi flow works as follows:

1. `MortgageService.payReward()` stores a per-cycle reward for a witness address, regardless of whether the witness currently has voters: [1](#0-0) 

2. At the maintenance cycle boundary, `MaintenanceManager.doMaintenance()` calls `delegationStore.accumulateWitnessVi(curCycle, witness.createDbKey(), witness.getVoteCount())` for every witness, using the *current* (i.e., about-to-end) cycle's vote count: [2](#0-1) 

3. Inside `accumulateWitnessVi()`, if the witness's vote count is `0` for that cycle (even though `reward` for that cycle is non-zero), the method explicitly skips accumulating the delta into `Vi` — it only "forwards" the previous `Vi` unchanged: [3](#0-2) 

4. Voter rewards are computed exclusively from the `Vi` delta between cycles (`beginVi`/`endVi`) in `MortgageService.computeReward()` — never from the raw per-cycle `reward` value directly: [4](#0-3) 

Because `Vi` was never incremented for that cycle, any reward recorded for a witness with zero votes in that cycle can never be reflected in any voter's claimable balance. There is no other code path that later reclaims or redistributes this stranded per-cycle reward value — it is simply orphaned in the store forever. This mirrors the reported bug class exactly: `rewardPerToken()` in the external report returns `rewardPerTokenStored` unchanged when `_totalSupply == 0`, silently dropping the reward accrued during that window; here `accumulateWitnessVi()` silently drops the reward accrued during a cycle when `voteCount == 0`, because it "forwards" `Vi` instead of accounting for the reward.

### Impact Explanation
Any block reward or transaction-fee reward paid to a witness that has zero votes for the cycle in which the reward was earned becomes permanently stranded and unclaimable by any account (medium-severity loss of funds, matching the acknowledged severity of the referenced report). Since witnesses can lose all votes over time (e.g., unfreezing/withdrawal of votes, or genuinely low-vote witnesses that still produce blocks in edge/test/private-chain configurations, or during transitions of witness sets), this is a reachable state through ordinary, unprivileged blockchain activity (vote withdrawal via `UnfreezeBalanceV2` contracts, and normal witness block production) — no privileged or malicious-SR access is required.

### Likelihood Explanation
The condition (`voteCount == 0` for a witness in a given cycle while it still earns block/fee rewards) can arise from entirely permissionless activity: unfreezing/withdrawing votes en masse for a witness (an ordinary `UnfreezeBalanceV2Contract` or `WithdrawBalanceContract` operation) can drop `witness.getVoteCount()` to zero while the witness is still actively producing blocks and earning `payBlockReward`/`payTransactionFeeReward` for the current cycle before the cycle rolls over. This is a naturally occurring state, not an attacker-crafted edge case requiring special privileges, making it moderately likely in long-running or low-participation witness scenarios.

### Recommendation
When `accumulateWitnessVi()` (or the calling `doMaintenance()` logic) detects `reward > 0` but `voteCount == 0` for a cycle, do not silently drop the reward. Options include: rolling the un-distributable reward forward into the next cycle's reward pool for the same witness (so it is redistributed once/if votes resume), or reallocating it to the network reward pool (e.g., `WitnessStandbyAllowance`) instead of leaving it permanently orphaned in `delegationStore`. This mirrors the "implement a reward reclaim mechanism" recommendation from the referenced report.

### Proof of Concept
1. Witness `W` produces blocks/collects fees during cycle `N` while it still has a positive `voteCount`, causing `payReward()` to call `delegationStore.addReward(N, W, value)` (increasing stored reward for cycle `N`) — see `MortgageService.payReward()`.
2. Before the maintenance cycle for `N` executes, all voters withdraw their votes from `W` via `UnfreezeBalanceV2Contract`/vote withdrawal, driving `W.getVoteCount()` to `0` for the remainder of cycle `N`.
3. When `MaintenanceManager.doMaintenance()` runs at the end of cycle `N`, it calls `delegationStore.accumulateWitnessVi(N, W, 0)` since `witness.getVoteCount()` is now `0`.
4. Inside `accumulateWitnessVi()`, because `voteCount == 0`, the branch that computes `deltaVi = reward * DECIMAL / voteCount` is skipped; `Vi` for cycle `N` is simply set to the previous cycle's `Vi` value, unchanged.
5. The reward stored via `addReward(N, W, value)` in step 1 is never referenced again by any computation path (`computeReward()` only ever reads `Vi` deltas), so it remains permanently unclaimable by any voter, even if new voters later vote for `W`.

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

**File:** chainbase/src/main/java/org/tron/core/service/MortgageService.java (L215-230)
```java
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
