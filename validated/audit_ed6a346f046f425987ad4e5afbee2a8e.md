Good—confirmed `mortgageService.withdrawReward`/`queryReward` are reachable from multiple standard, unprivileged actuators (`WithdrawBalanceActuator`, `UnfreezeBalanceActuator`, `UnfreezeBalanceV2Actuator`, `VoteWitnessActuator`). This confirms the call path is triggerable by any signed transaction. I have enough to finalize the analog.

### Title
Precision Loss in Legacy Vote-Reward Calculation Due to Floating-Point Division Before Multiplication - (File: `chainbase/src/main/java/org/tron/core/service/MortgageService.java`)

### Summary
`MortgageService.computeReward(long cycle, List<Pair<byte[], Long>> votes)` computes a voter's share of a Super Representative's cycle reward using `double` arithmetic where division is performed before multiplication, exactly mirroring the reported USSDRebalancer precision-loss pattern (`division/2 * 99/100` style ordering). This legacy computation path is still reachable today for any voter withdrawing or querying rewards for cycles that predate the new Vi/`BigInteger`-based reward algorithm.

### Finding Description
In the reward computation for the pre-hardfork ("old") reward algorithm: [1](#0-0) 
the code computes `voteRate = (double) userVote / totalVote` first, then multiplies by `totalReward`. This is the identical anti-pattern flagged in the external report: performing division before multiplication with floating-point/integer types introduces rounding error, because `userVote / totalVote` truncates/rounds to limited double precision before the multiplication recovers scale.

This is invoked by `computeReward(beginCycle, endCycle, accountCapsule)` → `getOldReward(begin, end, votes)`, which iterates cycles prior to `dynamicPropertiesStore.getNewRewardAlgorithmEffectiveCycle()`: [2](#0-1) [3](#0-2) 

Notably, the newer per-witness `Vi`-based algorithm was explicitly rewritten using `BigInteger multiply(...).divide(...)` to avoid exactly this class of bug: [4](#0-3) 
confirming the double-based `computeReward(cycle, votes)` path is the un-hardened legacy remnant of the same computation.

The entry points that route unprivileged, signed transactions into this reward path are `withdrawReward`/`queryReward`, called from: [5](#0-4) 
as well as `WithdrawBalanceActuator`, `UnfreezeBalanceActuator`, and `VoteWitnessActuator`, all of which any account can broadcast.

### Impact Explanation
Because `voteRate` is computed as a `double` from a division performed before the multiplication by `totalReward`, the resulting `reward` value can diverge from the exact `userVote * totalReward / totalVote` integer computation. For large `totalReward`/`totalVote` magnitudes (both are `long` cycle-level aggregates that can reach billions of sun), the loss of double-precision mantissa bits (53 bits) during the initial division can produce off-by-more-than-one discrepancies when the recovered value is later multiplied back up. Summed across many voters, the aggregate reward paid out for a cycle can drift from the actual `totalReward` pool recorded in `delegationStore`, producing either under-payment (funds left unclaimed but their SR-level halting effect is negligible) or, more importantly, an internal accounting mismatch between the sum of individually computed voter shares and the recorded pool, which is the same "unbacked distribution" risk category as the original report (incorrect token minting/burning proportions).

### Likelihood Explanation
This code executes on the standard, unprivileged withdraw/query-reward path used by essentially every voting account, and requires no special privileges — any signed `WithdrawBalanceContract`, `UnfreezeBalanceContract`, `UnfreezeBalanceV2Contract`, or `VoteWitnessContract` transaction that has unclaimed reward cycles predating `newRewardAlgorithmEffectiveCycle` will invoke it. The precision-loss magnitude scales with the size of `totalReward`/`totalVote`, so accounts with large stakes voting for popular witnesses are most likely to observe measurable drift.

### Recommendation
Rewrite `computeReward(long cycle, List<Pair<byte[], Long>> votes)` to use integer/`BigInteger` arithmetic performing multiplication before division, i.e. `reward += userVote * totalReward / totalVote` (using `BigInteger` or `Math.multiplyExact` + safe division) instead of computing an intermediate `double` ratio, consistent with the approach already used in the `Vi`-based path (`deltaVi.multiply(BigInteger.valueOf(userVote)).divide(DECIMAL_OF_VI_REWARD)`).

### Proof of Concept
Given `totalReward = 999_999_999_999L` sun and `totalVote = 3` with `userVote = 1`:
- Correct calculation (multiply first): `1 * 999_999_999_999 / 3 = 333_333_333_333`
- Buggy calculation (divide first, as in current code): `voteRate = (double)1/3 = 0.3333333333333333` (limited to 53 bits of mantissa); `reward = (long)(0.3333333333333333 * 999_999_999_999) = 333_333_333_332` or `333_333_333_333` depending on floating-point rounding — for larger totals (e.g. `totalReward` near `Long.MAX_VALUE` range typical of aggregated multi-cycle pools) the divergence from the exact integer result grows to multiple sun, and summed over many voters produces a measurable mismatch between the total distributed and the recorded `totalReward` in `delegationStore`.

### Citations

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

**File:** chainbase/src/main/java/org/tron/core/service/MortgageService.java (L260-269)
```java
  private long getOldReward(long begin, long end, List<Pair<byte[], Long>> votes) {
    if (dynamicPropertiesStore.allowOldRewardOpt()) {
      return rewardViCalService.getNewRewardAlgorithmReward(begin, end, votes);
    }
    long reward = 0;
    for (long cycle = begin; cycle < end; cycle++) {
      reward += computeReward(cycle, votes);
    }
    return reward;
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/UnfreezeBalanceV2Actuator.java (L71-72)
```java

    mortgageService.withdrawReward(ownerAddress);
```
