### Title
Genesis/first-cycle vote reward permanently lost due to unset `witnessVote` snapshot in `MortgageService.computeReward()` - (File: `chainbase/src/main/java/org/tron/core/service/MortgageService.java`)

### Summary
This is the same bug class as the SubscribeRegistry report: a reward that has already been *earned and recorded* is silently dropped from distribution because a "total supply" style guard (here, the per-cycle recorded total vote count for a witness) has not yet been initialized/non-zero, and the check causes the code to skip crediting rather than defer or fall back. In java-tron's legacy (pre-Vi) reward algorithm, this manifests for the very first reward cycle (cycle 0, before the first `doMaintenance()` maintenance run), analogous to "the first subscriber after TGE".

### Finding Description
Block/standby rewards earned by a witness are recorded unconditionally via `delegationStore.addReward(cycle, witnessAddress, value)`, called from `MortgageService.payReward()`: [1](#0-0) 

A voter later claims their share of that reward through `computeReward(long cycle, List<Pair<byte[], Long>> votes)`, which requires the per-cycle snapshot of the witness's total vote count: [2](#0-1) 

```
long totalVote = delegationStore.getWitnessVote(cycle, srAddress);
if (totalVote == DelegationStore.REMARK || totalVote == 0) {
  continue;
}
```

`delegationStore.getWitnessVote(cycle, address)` returns the sentinel `REMARK` (-1) whenever no snapshot exists for that cycle: [3](#0-2) 

The snapshot for a cycle is only ever written by `MaintenanceManager.doMaintenance()`, and it writes the vote-count snapshot for the **next** cycle, not the currently-running one: [4](#0-3) 

```
long nextCycle = dynamicPropertiesStore.getCurrentCycleNumber() + 1;
...
delegationStore.setWitnessVote(nextCycle, witness.createDbKey(), witness.getVoteCount());
```

As a result, for cycle `0` — the period between chain genesis and the first `doMaintenance()` run — `delegationStore.getWitnessVote(0, srAddress)` has never been written for any witness, so it always returns `REMARK`. Meanwhile, genesis/active witnesses already produce blocks and accrue rewards in cycle 0 via `payBlockReward`/`payStandbyWitness` → `delegationStore.addReward(0, address, value)`. Any account that casts a `VoteWitnessContract` for that witness and later calls `computeReward`/`withdrawReward` for cycle 0 will hit the `totalVote == REMARK` branch and `continue`, permanently skipping that cycle's reward share — exactly like the pool reward in the report that remains stuck because `StakingVault.totalSupply() == 0` at the time of the first deposit.

The reward itself is not "stuck" as a spendable balance anywhere (unlike the WETH in `SubscribeRegistry`) — it is recorded in `delegationStore` under the reward key for cycle 0/witness, but no voter's `computeReward` path can ever retrieve it (the check is `continue`, not "retry later"), and no other code path revisits cycle 0 to redistribute it. It is effectively burned/orphaned value that voters were entitled to but can never withdraw.

### Impact Explanation
This causes a permanent, protocol-level loss of legitimately earned voting rewards for every voter who casts votes in the network's very first reward cycle (cycle 0), on any java-tron deployment (mainnet at launch, or any new private/test network) before `useNewRewardAlgorithm()`'s Vi-based accounting takes over. This is a "permanent freezing/loss of funds" class impact: TRX reward value that should have accrued to voters is silently and unrecoverably dropped, with no attacker action needed beyond ordinary `VoteWitnessContract` transactions during the affected window.

### Likelihood Explanation
Likelihood is high for any freshly-bootstrapped java-tron network (new private chain, testnet, or the historical mainnet launch): any ordinary user who submits a `VoteWitnessContract` transaction during cycle 0 (before the very first `doMaintenance()` execution) is unknowingly guaranteed to lose their share of that cycle's reward. No special privilege or malicious intent is required — it is an unavoidable consequence of the vote-count snapshot being written one cycle late relative to genesis. On long-lived mainnet, this window has already passed, but it reproduces deterministically on any new network deployment.

### Recommendation
Either (a) seed `delegationStore.setWitnessVote(0, address, voteCount)` for all genesis witnesses during genesis initialization (`Manager.initWitness()`), so cycle 0 has a valid snapshot instead of `REMARK`, or (b) change `computeReward`'s handling of `REMARK` so that it does not silently `continue` and drop the reward, but instead falls back to a computable total (e.g., the live `WitnessCapsule.getVoteCount()` at query time) when no snapshot exists for the queried cycle.

### Proof of Concept
1. Start a fresh java-tron network from genesis config (before `useNewRewardAlgorithm()` cycle and before the first `doMaintenance()` call), with `allowChangeDelegation` enabled.
2. Have account `A` freeze/stake TRX and submit `VoteWitnessContract` voting for a genesis witness `W` while `dynamicPropertiesStore.getCurrentCycleNumber() == 0`.
3. Let `W` produce blocks during cycle 0; observe `delegationStore.addReward(0, W, value)` being called via `MortgageService.payReward` (triggered from `Manager.payReward(BlockCapsule)`).
4. Let the first maintenance interval elapse so `doMaintenance()` runs, moving to cycle 1 and writing `delegationStore.setWitnessVote(1, W, voteCount)` — note cycle `0`'s vote snapshot was never written.
5. Call `mortgageService.withdrawReward(A)` / `queryReward(A)`. Confirm via `delegationStore.getReward(0, W)` (non-zero) and `delegationStore.getWitnessVote(0, W)` (returns `REMARK`) that `computeReward(0, votes)` hits the `totalVote == REMARK` branch and skips crediting `A`, i.e., `A`'s allowance never reflects the cycle-0 reward despite `W` having earned it.

(Note: I was unable to execute this scenario against a live node in this environment; the analysis is based on static code tracing of `MortgageService`, `DelegationStore`, and `MaintenanceManager` as shown above. A background Devin session with a runnable node/test harness would be needed to produce an executed, assertion-backed PoC equivalent to the Foundry test in the original report.)

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

**File:** chainbase/src/main/java/org/tron/core/store/DelegationStore.java (L73-84)
```java
  public void setWitnessVote(long cycle, byte[] address, long value) {
    put(buildVoteKey(cycle, address), new BytesCapsule(ByteArray.fromLong(value)));
  }

  public long getWitnessVote(long cycle, byte[] address) {
    BytesCapsule bytesCapsule = get(buildVoteKey(cycle, address));
    if (bytesCapsule == null) {
      return REMARK;
    } else {
      return ByteArray.toLong(bytesCapsule.getData());
    }
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
