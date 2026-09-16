### Title
Per-cycle SR block/standby rewards are permanently lost when a Super Representative has zero total votes for that cycle - ([File: chainbase/src/main/java/org/tron/core/store/DelegationStore.java])

### Summary
`DelegationStore.accumulateWitnessVi()` mirrors the Synthetix-style "reward-per-token" pattern flagged in the external report: it correctly special-cases the zero-stake condition by not accruing new value into the cumulative reward index (`Vi`), but it still lets the per-cycle checkpoint advance and silently discards the reward amount that was already credited to that cycle via `MortgageService.addReward`. Because block rewards are paid to whichever witness produces the block regardless of that witness's vote count for the cycle, any cycle in which a witness temporarily has zero votes causes its block reward for that cycle to be earmarked (`delegationStore.addReward`) but never actually distributable to any voter, permanently disappearing from the reward accounting.

### Finding Description
`MortgageService.payBlockReward()` unconditionally credits the fixed per-block reward to whichever witness produced the block: [1](#0-0) 

This calls `DelegationStore.addReward(cycle, witnessAddress, value)`, which simply stores/accumulates the numeric reward for `(cycle, address)` with no check on whether that witness had any votes during the cycle: [2](#0-1) 

At the end of each maintenance cycle, `MaintenanceManager.doMaintenance()` calls `accumulateWitnessVi` for every witness using the "new reward algorithm": [3](#0-2) 

`accumulateWitnessVi` explicitly special-cases the zero-vote condition — exactly like the `totalSupply == 0` branch in the reported Solidity bug — by *not* folding the reward into the cumulative `Vi` index, but it still advances the per-cycle checkpoint (forwarding `preVi` unchanged) instead of preserving the un-distributed reward for a future cycle when votes exist again: [4](#0-3) 

The legacy reward algorithm has the identical flaw: `MortgageService.computeReward(cycle, votes)` skips (`continue`) any cycle where `totalVote == 0`, so the `totalReward` value stored via `addReward` for that witness/cycle is never paid to anyone: [5](#0-4) 

Because a witness's vote count is a live, user-controlled quantity (users can freely vote/unvote via `VoteWitnessContract` or vote through the TVM precompile in `VoteRewardUtil`), it is entirely possible — with no privileged or malicious actor required — for an elected/active witness to have zero total votes recorded for a given cycle (e.g., right after all voters withdraw or reallocate their votes, or before the maintenance cycle picks up newly cast votes) while it still produces blocks and continues to accrue `WitnessPayPerBlock` and `TransactionFeePool` rewards for that cycle. Those rewards are recorded in `delegationStore` but can never be attributed to `Vi` (new algorithm) or claimed via `computeReward` (old algorithm), so they are permanently unrecoverable by any voter.

### Impact Explanation
Reward amounts that are already accounted for (`addReward`) as belonging to a cycle become permanently non-distributable once the cycle boundary is crossed with a zero total vote count, because neither the new `Vi`-based algorithm nor the legacy `computeReward` algorithm ever revisits or carries forward that specific cycle's stored reward. This is a genuine, permanent loss of protocol-issued reward funds that should have gone to voters — the same "computed but forever undistributable" class of bug described in the external report, just realized in TRON's DPoS voting-reward subsystem (`DelegationStore`, `MortgageService`, `MaintenanceManager`) instead of a Solidity staking contract.

### Likelihood Explanation
This requires no malicious actor: normal user behavior (unvoting/rebalancing votes near a maintenance-cycle boundary, or votes not yet reflected for a newly-active witness) is sufficient to create a cycle where an active/producing witness has `voteCount == 0`, triggering the lossy path in both `accumulateWitnessVi` and `computeReward`. The condition is reachable purely through ordinary vote/unvote transactions broadcast by unprivileged accounts.

### Recommendation
When `voteCount == 0` (or `totalVote == 0` in the legacy path) but `reward > 0` for a cycle, do not silently drop the reward. Instead, either (a) roll the un-distributable reward amount forward into the next cycle's `addReward` bucket for the same witness so it becomes claimable once votes exist again, or (b) reject/skip crediting block/standby rewards to a witness for cycles in which it has zero recorded votes, consistent with the guard already present in `payStandbyWitness` (`voteSum < 1` check).

### Proof of Concept
1. Witness `W` is active (already elected) and scheduled to produce blocks in cycle `N`.
2. All accounts holding votes for `W` unvote (via `VoteWitnessContract` with 0 count, or `clearVote` through the TVM vote precompile) before the cycle-`N` maintenance boundary, so `witness.getVoteCount() == 0` and `delegationStore.setWitnessVote(N, W, 0)` is recorded in `doMaintenance` (`consensus/src/main/java/org/tron/consensus/dpos/MaintenanceManager.java:157-161`).
3. `W` still produces a block in cycle `N` before it is replaced in the active set; `Manager.payReward` → `MortgageService.payBlockReward` → `DelegationStore.addReward(N, W, blockPay)` credits `blockPay` sun to `(N, W)` (`chainbase/src/main/java/org/tron/core/service/MortgageService.java:69-87`, `chainbase/src/main/java/org/tron/core/store/DelegationStore.java:35-44`).
4. At the next maintenance run, `accumulateWitnessVi(N, W, 0)` is invoked; since `voteCount == 0`, the branch at `chainbase/src/main/java/org/tron/core/store/DelegationStore.java:136-139` merely forwards `preVi` — `blockPay` is never folded into `Vi`.
5. No voter's `computeReward` call (new or legacy algorithm) can ever retrieve this cycle's `blockPay` for `W`, since neither algorithm revisits a skipped cycle — the reward is permanently unclaimable.

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

**File:** chainbase/src/main/java/org/tron/core/store/DelegationStore.java (L35-44)
```java
  public void addReward(long cycle, byte[] address, long value) {
    byte[] key = buildRewardKey(cycle, address);
    BytesCapsule bytesCapsule = get(key);
    if (bytesCapsule == null) {
      put(key, new BytesCapsule(ByteArray.fromLong(value)));
    } else {
      put(key, new BytesCapsule(ByteArray
          .fromLong(ByteArray.toLong(bytesCapsule.getData()) + value)));
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

**File:** consensus/src/main/java/org/tron/consensus/dpos/MaintenanceManager.java (L96-101)
```java
    if (dynamicPropertiesStore.useNewRewardAlgorithm()) {
      long curCycle = dynamicPropertiesStore.getCurrentCycleNumber();
      consensusDelegate.getAllWitnesses().forEach(witness -> {
        delegationStore.accumulateWitnessVi(curCycle, witness.createDbKey(), witness.getVoteCount());
      });
    }
```
