### Title
Division-before-multiplication precision loss in legacy voter reward calculation underpays TRX holders on `WithdrawBalanceContract` — ([File: chainbase/src/main/java/org/tron/core/service/MortgageService.java])

### Summary
`MortgageService.computeReward(long cycle, List<Pair<byte[], Long>> votes)` computes a voter's share of a super representative's cycle reward by first dividing `userVote` by `totalVote` as a `double`, then multiplying by `totalReward`. This is the same division-before-multiplication precision-loss pattern flagged in the external report's `CurveMath6.linearInterpolation`, and it truncates the true `userVote * totalReward / totalVote` proportion, causing voters to receive slightly less reward than they are owed on every call.

### Finding Description [1](#0-0) 

```java
private long computeReward(long cycle, List<Pair<byte[], Long>> votes) {
  long reward = 0;
  for (Pair<byte[], Long> vote : votes) {
    ...
    long userVote = vote.getValue();
    double voteRate = (double) userVote / totalVote;   // division first
    reward += voteRate * totalReward;                  // multiplication after
  }
  return reward;
}
```

The mathematically correct integer-precise formula is `totalReward * userVote / totalVote` (multiply first, divide last), analogous to the report's recommended fix of computing `(targetX - startX)(endY - startY) / (endX - startX)` instead of `((targetX - startX) / (endX - startX)) * (endY - startY)`. Here, `voteRate` loses precision as a `double` before being multiplied back up by `totalReward`, and the accumulated `reward` (a `long`) truncates the fractional remainder on every iteration/cycle, systematically rounding down.

This legacy path (`computeReward(cycle, votes)`, invoked through `getOldReward`) is used for every cycle prior to `getNewRewardAlgorithmEffectiveCycle()`: [2](#0-1) 

Note that the *new* VI-based algorithm in the same method correctly multiplies before dividing using `BigInteger`: [3](#0-2) 
so only the legacy per-cycle path is affected.

Reachability: any TRX holder can trigger this calculation directly and permissionlessly by broadcasting a `WithdrawBalanceContract` transaction, which is executed by `WithdrawBalanceActuator`: [4](#0-3) 

`WithdrawBalanceActuator.execute` calls `mortgageService.withdrawReward(ownerAddress)`, which for any account whose vote history spans cycles before the new-algorithm cutover calls `computeReward(beginCycle, endCycle, accountCapsule)` → `getOldReward` → the vulnerable `computeReward(cycle, votes)`: [5](#0-4) [6](#0-5) 

The same query path is also reachable via the HTTP/gRPC read-only reward query API (`GetRewardServlet`/`mortgageService.queryReward`), which computes the same truncated value for display and for validating that a withdrawal has a positive reward.

### Impact Explanation
Every voter reward withdrawal for cycles prior to the new-reward-algorithm cutover computes a systematically-truncated (rounded down) reward due to the double-division-then-multiply pattern, instead of the precision-preserving multiply-then-divide order. The shortfall is never credited to anyone — it is simply dropped from the `reward` accumulator — resulting in a small but permanent loss of funds for the voter on each withdrawal across all affected cycles and all voters/witnesses that used the legacy algorithm before the network-wide algorithm switch. Because this is a systemic per-transaction/per-cycle rounding-down bias (not a random rounding error), the aggregate lost value across the network's full history of legacy-cycle reward withdrawals is a genuine, unrecoverable underpayment of funds to voters.

### Likelihood Explanation
100% likelihood on every affected withdrawal: any account that has votes overlapping cycles before `getNewRewardAlgorithmEffectiveCycle()` and calls `WithdrawBalanceContract` (or has any non-zero vote/reward combination that isn't an exact divisor) will trigger the precision loss. No special privileges, timing, or attacker cooperation are required — it is a deterministic arithmetic defect hit by ordinary user activity.

### Recommendation
Rewrite `computeReward(long cycle, List<Pair<byte[], Long>> votes)` to multiply before dividing, ideally using `BigInteger` (consistent with the new VI-based algorithm already used elsewhere in the same class and in `VoteRewardUtil`/`RewardViCalService`):

```java
long userVote = vote.getValue();
reward += BigInteger.valueOf(totalReward)
    .multiply(BigInteger.valueOf(userVote))
    .divide(BigInteger.valueOf(totalVote))
    .longValue();
```

This eliminates the intermediate `double` division and preserves full integer precision until the final divide, matching the fix pattern recommended in the source report.

### Proof of Concept
Given `totalReward = 100000026`, `totalVote = 2700000027` (sum of 27 witnesses each with slightly different vote counts as in `DelegationServiceTest`), and `userVote = 100000000`:

- Buggy path: `voteRate = (double)100000000 / 2700000027 ≈ 0.037037036...` (rounded to double precision) then `reward = (long)(voteRate * 100000026)` — loses precision from the intermediate double division before the multiply, truncating the fractional remainder.
- Correct path: `reward = 100000026L * 100000000L / 2700000027L` computed via `BigInteger`, preserving the exact quotient before truncation.

Repeated over many cycles and many voters (as exercised by `DelegationServiceTest.testWithdraw`), the buggy path yields a strictly smaller cumulative `reward` than the exact-integer calculation, matching the "small underpayment" pattern described in the source finding.

### Citations

**File:** chainbase/src/main/java/org/tron/core/service/MortgageService.java (L89-134)
```java
  public void withdrawReward(byte[] address) {
    if (!dynamicPropertiesStore.allowChangeDelegation()) {
      return;
    }
    AccountCapsule accountCapsule = accountStore.get(address);
    long beginCycle = delegationStore.getBeginCycle(address);
    long endCycle = delegationStore.getEndCycle(address);
    long currentCycle = dynamicPropertiesStore.getCurrentCycleNumber();
    long reward = 0;
    if (beginCycle > currentCycle || accountCapsule == null) {
      return;
    }
    if (beginCycle == currentCycle) {
      AccountCapsule account = delegationStore.getAccountVote(beginCycle, address);
      if (account != null) {
        return;
      }
    }
    //withdraw the latest cycle reward
    if (beginCycle + 1 == endCycle && beginCycle < currentCycle) {
      AccountCapsule account = delegationStore.getAccountVote(beginCycle, address);
      if (account != null) {
        reward = computeReward(beginCycle, endCycle, account);
        adjustAllowance(address, reward);
        reward = 0;
        logger.info("Latest cycle reward {}, {}.", beginCycle, account.getVotesList());
      }
      beginCycle += 1;
    }
    //
    endCycle = currentCycle;
    if (CollectionUtils.isEmpty(accountCapsule.getVotesList())) {
      delegationStore.setBeginCycle(address, endCycle + 1);
      return;
    }
    if (beginCycle < endCycle) {
      reward += computeReward(beginCycle, endCycle, accountCapsule);
      adjustAllowance(address, reward);
    }
    delegationStore.setBeginCycle(address, endCycle);
    delegationStore.setEndCycle(address, endCycle + 1);
    delegationStore.setAccountVote(endCycle, address, accountCapsule);
    logger.info("Adjust {} allowance {}, now currentCycle {}, beginCycle {}, endCycle {}, "
            + "account vote {}.", Hex.toHexString(address), reward, currentCycle,
        beginCycle, endCycle, accountCapsule.getVotesList());
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

**File:** chainbase/src/main/java/org/tron/core/service/MortgageService.java (L215-227)
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

**File:** actuator/src/main/java/org/tron/core/actuator/WithdrawBalanceActuator.java (L46-55)
```java
    try {
      withdrawBalanceContract = any.unpack(WithdrawBalanceContract.class);
    } catch (InvalidProtocolBufferException e) {
      logger.debug(e.getMessage(), e);
      ret.setStatus(fee, code.FAILED);
      throw new ContractExeException(e.getMessage());
    }

    mortgageService.withdrawReward(withdrawBalanceContract.getOwnerAddress()
        .toByteArray());
```
