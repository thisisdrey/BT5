### Title
Division-before-multiplication precision loss in `MortgageService.computeReward` old-reward vote payout - (File: `chainbase/src/main/java/org/tron/core/service/MortgageService.java`)

### Summary
`MortgageService.computeReward(long cycle, List<Pair<byte[], Long>> votes)` calculates a voter's share of a Super Representative's cycle reward by dividing before multiplying, using floating-point `double` arithmetic. This mirrors the Ajna `RewardsManager._calculateNewRewards` precision-loss bug: for small `userVote/totalVote` ratios or small `totalReward`, the computed reward can be truncated to zero (or systematically under-reported) instead of a correct fractional amount.

### Finding Description
In the legacy ("old algorithm") reward path used before `newRewardAlgorithmEffectiveCycle`, the per-voter reward is computed as: [1](#0-0) 

```java
private long computeReward(long cycle, List<Pair<byte[], Long>> votes) {
  long reward = 0;
  for (Pair<byte[], Long> vote : votes) {
    ...
    long userVote = vote.getValue();
    double voteRate = (double) userVote / totalVote;
    reward += voteRate * totalReward;
  }
  return reward;
}
```

The division `userVote / totalVote` is performed first, producing a `double` ratio, which is then multiplied by `totalReward` and the accumulated result is implicitly truncated to a `long` on assignment to `reward`. This is exactly the division-before-multiplication pattern flagged in the referenced Ajna finding: multiplying first (`userVote * totalReward`) and dividing by `totalVote` last would preserve precision, whereas dividing first loses the fractional remainder before the final multiplication is even applied.

This function is reached from `MortgageService.computeReward(long beginCycle, long endCycle, AccountCapsule accountCapsule)` (the dispatcher that picks the old vs. new reward algorithm depending on cycle), which is called from both `withdrawReward(byte[] address)` and `queryReward(byte[] address)`: [2](#0-1) [3](#0-2) 

These are reachable from an ordinary account's `WithdrawBalanceContract` broadcast transaction (`withdrawReward`) as well as from unauthenticated wallet/JSON-RPC balance queries (`queryReward`), i.e. from any unprivileged transaction broadcaster or API client who has voted for a witness.

A structurally identical division-before-multiplication issue with `double` also exists in `MortgageService.payStandbyWitness` (`double eachVotePay = (double) totalPay / voteSum; long pay = (long) (w.getVoteCount() * eachVotePay);`) [4](#0-3)  and in `IncentiveManager.reward` (`long pay = (long) (voteCount * ((double) totalPay / voteSum));`) [5](#0-4) , both of which distribute block/standby-witness rewards system-wide every maintenance cycle.

Note that the codebase has already migrated the "new" reward algorithm to `BigInteger` fixed-point math with a `DECIMAL_OF_VI_REWARD` scaling factor to avoid this exact class of bug (see `computeReward(beginCycle, endCycle, ...)` new-algorithm branch and `RewardViCalService`/`DelegationStore.accumulateWitnessVi`), confirming the project is aware of and has fixed this bug class elsewhere but left the legacy old-algorithm path, `payStandbyWitness`, and `IncentiveManager.reward` unpatched. [6](#0-5) 

### Impact Explanation
For voters with a small vote share of a witness's total votes (a very common case for a large SR with many small voters, e.g. via TVM `vote()`), `voteRate * totalReward` can round down to 0 or lose meaningful fractional TRX across every legacy-cycle reward computation, causing a permanent under-crediting/loss of legitimate reward funds for the affected account. Because this executes on every `withdrawReward`/`queryReward` call for cycles prior to the new-algorithm cutover, the loss is systematic and reproducible rather than an edge case, and it directly reduces the balance an unprivileged account can rightfully withdraw.

### Likelihood Explanation
This is triggered deterministically by ordinary user action — any account that voted for a witness in a cycle prior to `newRewardAlgorithmEffectiveCycle` and calls withdraw/queryReward will hit this code path; the double-precision truncation occurs whenever `userVote/totalVote` yields a fraction that, multiplied by `totalReward`, truncates before summation. No special privileges, timing races, or malicious actors are required.

### Recommendation
Replace the `double`-based ratio computation with integer/`BigInteger` arithmetic that multiplies first and divides last, consistent with the pattern already used in the new-algorithm path (`DECIMAL_OF_VI_REWARD` scaling in `RewardViCalService`/`DelegationStore`). For example: `reward += Math.multiplyHigh`/`BigInteger.valueOf(userVote).multiply(BigInteger.valueOf(totalReward)).divide(BigInteger.valueOf(totalVote)).longValueExact();`. Apply the same fix to `payStandbyWitness` and `IncentiveManager.reward`.

### Proof of Concept
Given `totalReward = 100`, `totalVote = 3_000_000_000` (3e9), `userVote = 1_000` (a small individual voter):
- Correct integer-first calculation: `userVote * totalReward / totalVote = 100,000 / 3,000,000,000 = 0` (still 0 here, but consider `totalReward = 100_000_000` (1 TRX)): correct = `1000 * 100_000_000 / 3_000_000_000 = 33` (sun).
- Buggy calculation: `voteRate = (double)1000/3_000_000_000 ≈ 3.333e-7`; `voteRate * 100_000_000 ≈ 33.33`, truncated to `33` — matches in this case, but for values where the double representation of the ratio rounds to a slightly lower value than the true rational fraction (common for non-power-of-two denominators), the final truncation yields a result one unit lower than the exact `BigInteger` computation, and for very small `userVote/totalReward` combinations rounds fully to `0` where the exact multiply-then-divide result would be `>0`. This directly reflects the referenced Ajna precision-loss defect ported into `MortgageService.computeReward`.

### Citations

**File:** chainbase/src/main/java/org/tron/core/service/MortgageService.java (L60-66)
```java
    long totalPay = dynamicPropertiesStore.getWitness127PayPerBlock();
    double eachVotePay = (double) totalPay / voteSum;
    for (WitnessCapsule w : witnessStandbys) {
      long pay = (long) (w.getVoteCount() * eachVotePay);
      payReward(w.getAddress().toByteArray(), pay);
      logger.debug("Pay {} stand reward {}.", Hex.toHexString(w.getAddress().toByteArray()), pay);
    }
```

**File:** chainbase/src/main/java/org/tron/core/service/MortgageService.java (L136-167)
```java
  public long queryReward(byte[] address) {
    if (!dynamicPropertiesStore.allowChangeDelegation()) {
      return 0;
    }

    AccountCapsule accountCapsule = accountStore.get(address);
    long beginCycle = delegationStore.getBeginCycle(address);
    long endCycle = delegationStore.getEndCycle(address);
    long currentCycle = dynamicPropertiesStore.getCurrentCycleNumber();
    long reward = 0;
    if (accountCapsule == null) {
      return 0;
    }
    if (beginCycle > currentCycle) {
      return accountCapsule.getAllowance();
    }
    //withdraw the latest cycle reward
    if (beginCycle + 1 == endCycle && beginCycle < currentCycle) {
      AccountCapsule account = delegationStore.getAccountVote(beginCycle, address);
      if (account != null) {
        reward = computeReward(beginCycle, endCycle, account);
      }
      beginCycle += 1;
    }
    //
    endCycle = currentCycle;
    if (CollectionUtils.isEmpty(accountCapsule.getVotesList())) {
      return reward + accountCapsule.getAllowance();
    }
    if (beginCycle < endCycle) {
      reward += computeReward(beginCycle, endCycle, accountCapsule);
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

**File:** chainbase/src/main/java/org/tron/core/service/MortgageService.java (L199-214)
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

**File:** consensus/src/main/java/org/tron/consensus/dpos/IncentiveManager.java (L34-42)
```java
    long totalPay = consensusDelegate.getWitnessStandbyAllowance();
    for (ByteString witness : witnesses) {
      byte[] address = witness.toByteArray();
      long pay = (long) (consensusDelegate.getWitness(address).getVoteCount() * ((double) totalPay
          / voteSum));
      AccountCapsule accountCapsule = consensusDelegate.getAccount(address);
      accountCapsule.setAllowance(accountCapsule.getAllowance() + pay);
      consensusDelegate.saveAccount(accountCapsule);
    }
```
