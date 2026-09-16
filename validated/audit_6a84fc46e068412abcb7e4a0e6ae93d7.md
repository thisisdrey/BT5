### Title
Witness block/transaction-fee rewards become permanently unclaimable when a witness has zero total votes in a cycle - ([File: chainbase/src/main/java/org/tron/core/service/MortgageService.java])

### Summary
When a Super Representative (witness) produces a block, its block reward and transaction-fee reward are unconditionally credited into the per-cycle, per-witness reward bucket in `DelegationStore` via `MortgageService.payReward` [1](#0-0) . This reward is only ever distributed to voters proportionally to their share of that witness's total votes for that cycle, computed in `MortgageService.computeReward(long cycle, List<Pair<byte[], Long>> votes)` [2](#0-1) . If the witness's recorded `totalVote` for that cycle is `0`, the loop simply `continue`s and skips crediting the reward to anyone — it is neither returned to the witness, brokerage, nor treasury; it just remains permanently stranded inside `DelegationStore`'s reward entry for that cycle/address, unreachable by any code path.

### Finding Description
`MortgageService.payReward` is invoked from `Manager.payReward` on every produced block, crediting the witness's block reward and (if enabled) transaction fee reward into `delegationStore.addReward(cycle, witnessAddress, value)` after deducting the brokerage share [1](#0-0) . This happens unconditionally for whichever witness is scheduled to produce the block — it does not check whether that witness actually has any recorded votes for the current cycle.

Separately, `MaintenanceManager.doMaintenance()` snapshots the vote count for the *next* cycle for every registered witness, including witnesses with zero votes, via `delegationStore.setWitnessVote(nextCycle, witness.createDbKey(), witness.getVoteCount())` over `consensusDelegate.getAllWitnesses()` [3](#0-2) . Any witness that is part of the active/producing set but has a `voteCount` of `0` at snapshot time (e.g., a genesis/private-chain SR that has never received a vote, or a witness whose votes were all withdrawn just before the cycle boundary) will have `totalVote == 0` recorded for that cycle.

When a voter later attempts to withdraw rewards, `computeReward(cycle, votes)` iterates each vote and explicitly skips (via `continue`) any SR whose `totalVote` for that cycle is `0` or equal to `DelegationStore.REMARK` [4](#0-3) . Since there are no voters to divide the reward among (division would be undefined / the SR has no vote-holders for that cycle), the previously credited `totalReward` value stored via `addReward` for that cycle/address is never read out or refunded anywhere else in the codebase — it is permanently orphaned in the `DelegationStore`.

This is structurally identical to the reported Y2K Earthquake bug: emissions/rewards are deposited into a bucket keyed by a period (epoch/cycle) and address, but if that bucket ends up with zero "shares" (stakers/voters) to divide by, the deposited funds are unrecoverable — no fallback path returns them to treasury or the depositor.

### Impact Explanation
The economic value of the block reward (and, if enabled, the transaction-fee-pool reward) for any cycle in which the producing witness has zero recorded votes is permanently lost — it is deducted from the network's reward-emission logic (already computed/allocated) but can never be withdrawn by any account, and is not returned to treasury/brokerage/witness. This is a permanent freezing of funds, which is realistic in private/consortium java-tron deployments (a common use of java-tron) where the number of registered witnesses is small and some SRs may run with zero external votes while still being scheduled to produce blocks by the DPoS committee.

### Likelihood Explanation
No privileged action is required to trigger this — it happens automatically whenever a witness produces a block while recorded as having zero votes for the current cycle, a state reachable through normal witness registration/voting/unvoting flows and common on small/private chains. It requires no malicious actor, no special permissions, and is purely a consequence of the reward accounting design.

### Recommendation
In `MortgageService`, when crediting a block/transaction-fee reward to a witness whose `delegationStore.getWitnessVote(cycle, witnessAddress)` is `0` (or `REMARK`), either (a) redirect the entire reward to the witness's own allowance/brokerage instead of the shared voter-reward bucket, or (b) return it to a treasury/fee pool, rather than calling `delegationStore.addReward` into a bucket that can never be divided among voters. Additionally, `MortgageService.computeReward` should not silently `continue` past a nonzero `totalReward` with zero `totalVote` without some compensating recovery path.

### Proof of Concept
1. Register a witness `W` with `voteCount == 0` (e.g. a genesis SR in a private network, or after all voters unfreeze/withdraw votes for `W` right before a maintenance cycle boundary).
2. `MaintenanceManager.doMaintenance()` runs at the cycle boundary and records `delegationStore.setWitnessVote(nextCycle, W, 0)` [5](#0-4) .
3. In the next cycle, `W` is scheduled and produces a block; `Manager.payReward` invokes `mortgageService.payBlockReward(W, getWitnessPayPerBlock())` [6](#0-5) , which calls `payReward` and stores `delegationStore.addReward(cycle, W, value)` [1](#0-0) .
4. No voter can ever redeem this cycle's reward for `W`, because any voter's `computeReward(cycle, votes)` call will see `totalVote == 0` for `W` at that cycle and `continue`, skipping the reward entirely [7](#0-6) .
5. The `value` credited in step 3 remains permanently stored under that cycle/address key in `DelegationStore` with no code path that ever reads or reclaims it.

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

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L1950-1953)
```java
    if (getDynamicPropertiesStore().allowChangeDelegation()) {
      mortgageService.payBlockReward(witnessCapsule.getAddress().toByteArray(),
          getDynamicPropertiesStore().getWitnessPayPerBlock());
      mortgageService.payStandbyWitness();
```
