### Title
Block/transaction-fee reward funds are permanently stuck when the receiving witness has zero registered votes for the cycle - ([File: chainbase/src/main/java/org/tron/core/service/MortgageService.java])

### Summary
`MortgageService.payReward()` unconditionally records incoming block/transaction-fee rewards into `DelegationStore` under `(cycle, witnessAddress)` without checking whether that witness has any recorded voters for the current cycle. When voters later compute their share of that reward, the computation divides by `totalVote` for that witness/cycle and explicitly skips (treats as zero) any cycle where the witness's vote count is `0` or unset (`REMARK`). This is the exact same "donate rewards with no active participants to receive them" bug class as the `Voter.notifyRewardAmount()` finding: funds are credited to a pool keyed by an entity that turns out to have no shareholders for that period, and there is no fallback/carry-over mechanism, so the funds become permanently unreachable.

### Finding Description
`MortgageService.payReward()` is invoked from `payBlockReward()` and `payTransactionFeeReward()`, both called from block application in `Manager.java` for every witness that produces a block or collects transaction fees, with no precondition on that witness's vote count for the cycle: [1](#0-0) 

The reward amount (after brokerage) is stored via `delegationStore.addReward(cycle, witnessAddress, value)`: [2](#0-1) 

The reward is later distributed to voters via `computeReward(cycle, votes)`, which retrieves `totalVote = delegationStore.getWitnessVote(cycle, srAddress)` and explicitly discards the entire reward for that cycle if `totalVote == DelegationStore.REMARK || totalVote == 0`: [3](#0-2) 

`getWitnessVote()` returns `REMARK` (`-1`) when no entry exists for that `(cycle, address)` key, and the per-cycle vote snapshot is only written during maintenance for currently-tracked witnesses: [4](#0-3) [5](#0-4) 

Once `computeReward` skips a cycle's reward due to `totalVote == 0`/`REMARK`, that specific `(cycle, witnessAddress)` reward entry in `DelegationStore` is never revisited by any other code path — there is no mechanism that carries the entry forward to a later cycle or refunds/redistributes it. Any TRX paid to the witness for that cycle (block reward, transaction-fee reward, or brokered standby pay entries created in a cycle where the witness's vote snapshot is `0`) is permanently orphaned in the account/reward store, unbacked by any account balance ever reachable through withdrawal. This mirrors the reported `Voter.notifyRewardAmount()` issue: a reward pool is funded for a "no voters" period and the distribution logic has an unconditional zero/skip branch instead of a carry-over or rejection check.

### Impact Explanation
Funds credited via `payBlockReward`/`payTransactionFeeReward` (which are unconditionally triggered by block production/transaction fee collection reachable from ordinary transaction broadcasting) can become permanently frozen and undistributable to any account, since the corresponding `(cycle, witnessAddress)` reward record is silently discarded once `getWitnessVote` returns `0`/`REMARK` for that cycle. This constitutes permanent freezing of protocol funds (loss of otherwise-distributable TRX), matching the "permanent freezing of funds" impact criterion.

### Likelihood Explanation
The precondition — a witness's vote-count snapshot for a given cycle being `0` or missing — can arise from ordinary vote-count timing/removal (e.g., a witness losing all votes within a cycle, or a witness added mid-cycle before `MaintenanceManager.doMaintenance()` records a vote snapshot for it) combined with the witness still producing blocks or collecting fees in that same cycle. This does not require any privileged or malicious actor; it is a naturally reachable state through routine block production and voting/un-voting activity of unprivileged accounts.

### Recommendation
In `MortgageService.payReward()` (and `IncentiveManager.reward()` for standby pay), check whether the target witness has a nonzero vote count for the current cycle before crediting the `DelegationStore` reward entry; if it is zero, either reject/skip the credit and instead route the funds to a general fund (e.g., burn or carry to `Manager`'s block-reward pool) or defer/accumulate the amount into the next cycle where the witness has recorded votes, ensuring `computeReward`'s zero-vote skip never causes value to vanish without a corresponding redirection.

### Proof of Concept
1. A witness `W` is actively producing blocks (or was just added to the active set) but currently has `voteCount == 0` (e.g., all voters withdrew their votes in the current cycle, or `W` was newly registered mid-cycle before the next `doMaintenance()` snapshot).
2. `W` produces a block; `Manager.java` calls `mortgageService.payBlockReward(W, value)` (and/or `payTransactionFeeReward`), which calls `payReward()`: [6](#0-5) 
3. This stores `value - brokerageAmount` into `delegationStore.addReward(currentCycle, W, value)`, with no check on `W`'s recorded vote count for `currentCycle`.
4. At `MaintenanceManager.doMaintenance()`, the vote snapshot for cycle `currentCycle+1` is set from `witness.getVoteCount()`, but the just-elapsed cycle's `delegationStore.getWitnessVote(currentCycle, W)` remains `0`/`REMARK` because no voter ever accumulated votes for `W` during that cycle: [5](#0-4) 
5. Any voter who later calls `withdrawReward`/`queryReward` and whose `computeReward` iterates over cycle `currentCycle` for witness `W` finds `totalVote == 0`, hits `continue`, and receives nothing for that cycle: [3](#0-2) 
6. The reward value stored at `(currentCycle, W)` in `DelegationStore` is never read again by any successful non-zero-vote path — it is permanently stuck, unbacked by any withdrawable account balance.

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

**File:** chainbase/src/main/java/org/tron/core/store/DelegationStore.java (L35-53)
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

  public long getReward(long cycle, byte[] address) {
    BytesCapsule bytesCapsule = get(buildRewardKey(cycle, address));
    if (bytesCapsule == null) {
      return 0L;
    } else {
      return ByteArray.toLong(bytesCapsule.getData());
    }
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
