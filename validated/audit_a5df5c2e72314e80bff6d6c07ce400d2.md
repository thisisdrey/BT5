### Title
Witness reward for a zero-vote SR is permanently lost instead of being carried forward to voters - ([File: chainbase/src/main/java/org/tron/core/store/DelegationStore.java])

### Summary
`DelegationStore.accumulateWitnessVi()`, used by java-tron's cycle-based delegation/voting reward mechanism, silently drops block/transaction-fee rewards that were paid to a super representative (SR) during a cycle in which that SR had zero recorded votes. Instead of carrying the un-distributable reward forward so it can be paid out once the SR gains voters, the funds are discarded from the accounting (the cumulative "Vi" index is not incremented), meaning the reward becomes permanently unclaimable by anyone.

### Finding Description
Every block, `MortgageService.payBlockReward()` / `payTransactionFeeReward()` / `payStandbyWitness()` add the witness's share of block/standby rewards into a per-cycle counter via `DelegationStore.addReward(cycle, witnessAddress, value)`: [1](#0-0) 

At the end of every maintenance cycle, `MaintenanceManager.doMaintenance()` folds each witness's accumulated reward for the just-finished cycle into a cumulative reward-per-vote index ("Vi") that is later used to compute each voter's share: [2](#0-1) 

The actual accumulation logic is in `DelegationStore.accumulateWitnessVi()`: [3](#0-2) 

If `reward > 0` but `voteCount == 0` for that witness in that cycle, the method takes the `if (reward == 0 || voteCount == 0)` branch and just "forwards" the previous cycle's Vi unchanged - it does **not** add `deltaVi` for the reward that was actually paid. Because the cycle's reward record (`DelegationStore.addReward`) is keyed to that specific past cycle and is never re-read or re-processed by any other code path, that reward value is never folded into any voter's Vi delta in the future. The reward is effectively vanished from the accounting: no voter's `computeReward()` calculation (in `MortgageService.computeReward()`/`VoteRewardUtil.computeReward()`, driven by `deltaVi = endVi - beginVi`) will ever reflect it, since `Vi` for that cycle equals the prior cycle's `Vi`.

This is the exact bug class from the external report: the contract "keeps track of the total time passed... [but] does not include such a condition for the tracking" of whether funds should actually be distributed - here, block rewards are paid to an SR and bookkept as "distributed for cycle N" even though there are zero voters (recipients) to receive a share, and the funds are lost rather than deferred to when a voter exists.

This scenario is reachable without any privileged action: it can occur for genesis witnesses or any SR that legitimately has (or drops to) zero votes in a cycle while still producing blocks and earning block/standby rewards (e.g., a witness whose voters fully withdrew/unvoted right before a cycle boundary, or a newly bootstrapped SR before it has attracted any votes). No malicious SR/witness behavior, network-level attack, or off-chain privilege is required — it is a consequence of normal `Manager` block application and the standard `VoteWitnessActuator`/`WithdrawRewardProcessor` vote/withdraw flow interacting with `MortgageService`/`VoteRewardUtil`.

### Impact Explanation
The reward paid to a witness during a zero-vote cycle becomes permanently stuck/unbacked in the delegation reward accounting: it was deducted from the reward pool as "paid" for that cycle, but is never assignable to any account's allowance, and no mechanism (admin or otherwise) can later reclaim or redistribute it, since the per-cycle reward record is only consumed once, at maintenance time, by `accumulateWitnessVi`. This constitutes a permanent freezing/loss of funds within the protocol's reward accounting, matching the Medium-severity impact class described in the reference report (funds intended for stakers/voters are silently lost rather than deferred).

### Likelihood Explanation
The trigger condition (`voteCount == 0` while `reward > 0` for a given witness/cycle) is a normal, permissionless outcome of standard voting/unvoting and block production — not a contrived or privileged edge case. Any SR whose entire voter base withdraws or who has not yet been voted for, yet still produces blocks or is in the standby-pay group, exercises this path automatically as part of `Manager`/`MaintenanceManager` block/cycle processing.

### Recommendation
When `voteCount == 0` but `reward > 0` in `accumulateWitnessVi`, do not silently forward the previous Vi. Instead, carry the un-distributed reward forward to the next cycle (e.g., re-add it via `addReward(cycle + 1, address, reward)` before accumulation) so that it is included once the witness gains voters, mirroring how the reference-report recommendation preserves undistributed emissions for future recipients rather than discarding them.

### Proof of Concept
1. Let witness `W` have `voteCount == 0` for cycle `N` (e.g., no one has voted for `W` yet, or all voters unvoted before the cycle boundary in `MaintenanceManager.doMaintenance()`, which snapshots `witness.getVoteCount()`).
2. During cycle `N`, `W` produces blocks; `Manager` calls `MortgageService.payBlockReward(W, value)`, which calls `DelegationStore.addReward(N, W, value)` — the DB now records `reward(N, W) = value > 0`.
3. At the end of cycle `N`, `MaintenanceManager.doMaintenance()` calls `delegationStore.accumulateWitnessVi(N, W, 0)` because `W.getVoteCount() == 0`.
4. In `accumulateWitnessVi`, `reward = getReward(N, W) = value`, `voteCount = 0` ⇒ branch `if (reward == 0 || voteCount == 0)` is taken ⇒ `setWitnessVi(N, W, preVi)` (unchanged) — `deltaVi` for `value` is never added.
5. Any subsequent voter who votes for `W` after cycle `N` can only earn rewards from `deltaVi` computed from `Vi` at cycle boundaries after they start voting; the reward `value` from cycle `N` is never reflected in any `Vi` delta and is unrecoverable by any account via `MortgageService.withdrawReward()`/`queryReward()` or `VoteRewardUtil.withdrawReward()`/`queryReward()`.

### Citations

**File:** chainbase/src/main/java/org/tron/core/service/MortgageService.java (L69-87)
```java
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
