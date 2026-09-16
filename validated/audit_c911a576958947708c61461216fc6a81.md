## Title
Witness block/transaction fee rewards become permanently locked when `witnessVote == 0` for a cycle - (File: `chainbase/src/main/java/org/tron/core/service/MortgageService.java`)

## Summary
The reported Y2K bug locks `emissionsToken` forever when `finalTVL == 0` because the withdrawal formula `entitledAmount = _assets.mulDivDown(emissions[_id], finalTVL[_id])` has no path to reclaim emissions when the TVL denominator is zero. The same structural flaw exists in java-tron's DPoS voting-reward accounting: block/transaction-fee rewards are unconditionally accrued to a witness's per-cycle reward bucket via `MortgageService.payReward`, but the payout formula that distributes that bucket to voters silently discards it whenever the recorded `totalVote` for that witness/cycle is `0`, with no fallback to reclaim or redistribute the value.

## Finding Description
Every time a witness produces a block or collects transaction fees, `MortgageService.payReward` computes a brokerage cut and stores the remainder into the witness's per-cycle reward pool unconditionally: [1](#0-0) 

This value is persisted via `DelegationStore.addReward`, independent of whether the witness has any recorded voter weight for that cycle: [2](#0-1) 

When a voter later calls `withdrawReward`/`queryReward`, the per-voter share of that reward pool is computed by `computeReward`, which explicitly skips (i.e., permanently forfeits) the reward whenever the witness's `totalVote` for the cycle is `0` (or `REMARK`): [3](#0-2) 

The same zero-guard pattern (skip rather than reclaim) also appears in the "new algorithm" Vi-accumulation paths used by `RewardViCalService` and `DelegationStore.accumulateWitnessVi`: [4](#0-3) [5](#0-4) 

In all of these, when the divisor (`totalVote`) is `0`, the code takes the `continue`/"just forward" branch instead of erroring or crediting the value back to any account. The reward amount recorded by `addReward` remains stored in `DelegationStore` under that `(cycle, witnessAddress)` key forever: no other code path reads it back out except through `computeReward`, which — once the zero-vote condition is hit for that cycle — will never again attribute that value to anyone. This mirrors the reported pattern exactly: an amount is deposited/credited into an accounting bucket (`emissionsToken` balance / `delegationStore` reward record) whose payout formula divides by a value (`finalTVL` / `totalVote`) that can legitimately be `0`, and there is no recovery mechanism, so the value is permanently stranded.

## Impact Explanation
This results in permanent freezing of a portion of witness/voter reward funds (TRX allowance) within the chain state: it is credited into `delegationStore`'s cycle-scoped reward key but can never be moved into any account's spendable `allowance` once the zero-vote condition occurs for that cycle, matching the "permanent freezing of funds" acceptance criterion. Because `payReward` is invoked automatically for every block a witness produces (reachable purely through normal DPoS block production/consensus reward flow, not through any privileged action), and `totalVote` snapshots are cycle-level state that voters can legitimately drive to zero by unvoting, this is reachable without any malicious-SR or privileged actor assumption.

## Likelihood Explanation
Occurrence requires a witness's recorded vote total for a specific reward cycle to be `0` while it still earned block/fee rewards during that cycle (e.g., voters withdrawing all votes from a witness around a cycle boundary, or the witness losing all votes while transitioning in/out of the active/standby set). This is a normal-usage edge case reachable by any voter withdrawing votes, not by an attacker exploiting a privileged role — it does not require compromising consensus, keys, or the p2p layer.

## Recommendation
When accruing rewards via `payReward`/`addReward`, or when accumulating Vi (`accumulateWitnessVi`), detect the case where a witness's per-cycle `totalVote` is `0` and either (a) redirect the reward directly to the witness's own allowance instead of the (unreachable) voter pool, or (b) roll it forward/into brokerage rather than silently discarding the associated payout share, so no reward amount becomes permanently unclaimable in `DelegationStore`.

## Proof of Concept
1. A witness accrues block reward for cycle `N` via `payReward`, which calls `delegationStore.addReward(N, witnessAddress, value)` — [1](#0-0) .
2. Before/without any snapshot recording nonzero `witnessVote` for cycle `N` (e.g., `getWitnessVote` returns `0` or `REMARK` for that witness/cycle) — [6](#0-5) .
3. Any voter (or the reward-Vi background service) calling `computeReward`/`accumulateWitnessVi` for cycle `N` hits the `totalVote == 0` branch and `continue`s, meaning the value added in step 1 is never attributed to any account's `allowance` — [3](#0-2) .
4. No other code path reads `delegationStore.getReward(N, witnessAddress)` back into a spendable balance, so the amount is permanently stranded in chain state.

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

**File:** chainbase/src/main/java/org/tron/core/store/DelegationStore.java (L77-84)
```java
  public long getWitnessVote(long cycle, byte[] address) {
    BytesCapsule bytesCapsule = get(buildVoteKey(cycle, address));
    if (bytesCapsule == null) {
      return REMARK;
    } else {
      return ByteArray.toLong(bytesCapsule.getData());
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
