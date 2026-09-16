Based on my analysis, I've identified a strong structural analog in java-tron's reward/vote (SR delegation) subsystem.

### Title
Witness/SR reward for a cycle is permanently stuck when the witness has zero votes at cycle-accumulation time - ([File: chainbase/src/main/java/org/tron/core/service/RewardViCalService.java])

### Summary
The reported bug (M-21) is a class of "distribute-without-checking-supply" issue: a program withdraws/accrues interest owed to depositors but never checks whether any depositor (bond holder) still exists to receive it, so the interest becomes permanently stuck. The same structural flaw exists in java-tron's Super Representative (witness) reward accounting: block/transaction-fee rewards are credited to a witness for the current voting cycle via `MortgageService.payReward()`, but when that cycle's Vi (reward-per-vote index) is accumulated, the code silently drops the reward if the witness's vote count is zero at that moment, with no fallback recipient and no way to reclaim it later.

### Finding Description
Block and transaction-fee rewards earned by an active witness are recorded per-cycle in `DelegationStore` via `MortgageService.payReward()`, which subtracts the brokerage share (paid immediately to the witness) and stores the remainder as the pool owed to voters: [1](#0-0) 

This voter-owed pool is only ever unlocked through the Vi (reward index) accumulation logic. In the real-time path used during `MaintenanceManager.doMaintenance()`, every witness's Vi is updated using its *current* vote count: [2](#0-1) 

which calls `DelegationStore.accumulateWitnessVi()`: [3](#0-2) 

and the equivalent historical/batch path in `RewardViCalService.accumulateWitnessVi()` behaves identically: [4](#0-3) 

In both implementations, `if (reward == 0 || voteCount == 0)` simply forwards the previous Vi unchanged instead of accumulating the reward — the `reward` value that was added to the store via `addReward()`/stored under the `"reward"` key is never converted into a claimable delta and is never refunded to the witness (via brokerage or otherwise). It is silently discarded.

A witness can legitimately have `voteCount == 0` for the cycle being finalized even though it earned block/tx-fee rewards during that cycle: it remains on the active/standby witness list (and keeps producing blocks and collecting fees) until the *next* maintenance cycle, while all of its voters can withdraw their votes (via `VoteWitnessActuator`/`UnfreezeBalanceV2Processor` → vote clearing) at any point during the current cycle. When maintenance runs, `witness.getVoteCount()` reflects the now-zero vote total, so the reward accrued for that cycle in `DelegationStore` (or `RewardViStore`) is stranded forever — no voter's `Vi` delta captures it, and there's no code path that later re-attributes or refunds this stuck reward. The parallel old-algorithm path in `MortgageService.computeReward()` has the same effect for pre-Vi cycles: if `totalVote == 0`, the reward is skipped (`continue`) with no owner: [5](#0-4) 

### Impact Explanation
This causes permanent, unrecoverable loss (freezing) of block reward / transaction fee funds that were earmarked for voters — the TRX remains debited from the global reward budget (paid out of `payStandbyWitness`/`payBlockReward`/`payTransactionFeeReward`) but is never credited to any account's `allowance`, making it permanently unbacked/stuck in the `DelegationStore` under an unreachable key. This is a concrete, permanent freezing-of-funds condition reachable purely by SR/witness reward accrual and normal user vote-withdrawal transactions — no privileged or malicious-actor role is required.

### Likelihood Explanation
This can occur under ordinary conditions: any witness that is active during a cycle (thus earning block/fee rewards) while all its voters happen to fully unvote before the maintenance boundary will trigger the loss. Because vote withdrawal is a normal, permissionless transaction (`VoteWitnessContract` with empty vote list, or resource unfreeze that clears votes) and does not require coordinating with block production, this is plausible in production, especially for lower-ranked/standby witnesses with few voters.

### Recommendation
Before dropping the reward when `voteCount == 0` in `DelegationStore.accumulateWitnessVi()` and `RewardViCalService.accumulateWitnessVi()` (and the analogous `MortgageService.computeReward()` old-algorithm branch), redirect the orphaned per-cycle reward — e.g., credit it to the witness's own `allowance` (as an extra brokerage) or roll it forward into the next cycle's reward pool for that witness — instead of silently discarding it.

### Proof of Concept
1. Witness `W` becomes active with non-zero votes and starts producing blocks; `payBlockReward`/`payTransactionFeeReward` call `payReward`, which stores the voter-share via `delegationStore.addReward(cycle, W, value)`.
2. All voters submit `VoteWitnessContract` transactions clearing their votes for `W` (or fully unfreeze) before the cycle's maintenance boundary, driving `W.getVoteCount()` to 0.
3. At the next `doMaintenance()`, `delegationStore.accumulateWitnessVi(curCycle, W, 0)` is invoked; since `voteCount == 0`, the branch that would consume the stored `reward` is skipped, and the reward recorded in step 1 is never turned into a Vi delta.
4. No voter (there are none) nor `W` itself can ever claim that reward through `MortgageService.withdrawReward()`/`queryReward()`, permanently stranding those funds. [1](#0-0) [3](#0-2) [4](#0-3) [2](#0-1)

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

**File:** chainbase/src/main/java/org/tron/core/service/MortgageService.java (L171-188)
```java
  private long computeReward(long cycle, List<Pair<byte[], Long>> votes) {
    long reward = 0;
    for (Pair<byte[], Long> vote : votes) {
      byte[] srAddress = vote.getKey();
      long totalReward = delegationStore.getReward(cycle, srAddress);
      if (totalReward <= 0) {
        continue;
      }
      long totalVote = delegationStore.getWitnessVote(cycle, srAddress);
      if (totalVote == DelegationStore.REMARK || totalVote == 0) {
        continue;
      }
      long userVote = vote.getValue();
      double voteRate = (double) userVote / totalVote;
      reward += voteRate * totalReward;
    }
    return reward;
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
