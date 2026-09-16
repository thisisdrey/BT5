### Title
Division-before-multiplication precision loss in legacy vote-reward calculation permanently underpays/freezes voter rewards - (File: `chainbase/src/main/java/org/tron/core/service/MortgageService.java`)

### Summary
`MortgageService.computeReward(long cycle, List<Pair<byte[], Long>> votes)` computes each voter's share of a Super Representative's cycle reward by first dividing `userVote` by `totalVote` into a `double`, then multiplying by `totalReward`. This is the exact division-before-multiplication pattern flagged in the external `FluxToken.sol::getClaimableFlux` report: performing the division first truncates/loses precision before the multiplication is applied, so the accumulated `long reward` value is systematically smaller (or otherwise inexact) than the mathematically correct `userVote * totalReward / totalVote`.

### Finding Description [1](#0-0) 

```java
private long computeReward(long cycle, List<Pair<byte[], Long>> votes) {
    long reward = 0;
    for (Pair<byte[], Long> vote : votes) {
      byte[] srAddress = vote.getKey();
      long totalReward = delegationStore.getReward(cycle, srAddress);
      if (totalReward <= 0) { continue; }
      long totalVote = delegationStore.getWitnessVote(cycle, srAddress);
      if (totalVote == DelegationStore.REMARK || totalVote == 0) { continue; }
      long userVote = vote.getValue();
      double voteRate = (double) userVote / totalVote;   // <-- division first
      reward += voteRate * totalReward;                  // <-- then multiply
    }
    return reward;
  }
```

This is the "old algorithm" path, invoked from `getOldReward()`: [2](#0-1) 

which is used by `computeReward(beginCycle, endCycle, accountCapsule)` whenever `beginCycle < newAlgorithmCycle` (i.e., any cycles predating the `NEW_REWARD_ALGORITHM_EFFECTIVE_CYCLE` activation): [3](#0-2) 

The double-precision `voteRate` truncates the fractional component of `userVote/totalVote` before scaling by `totalReward`, unlike the "new algorithm" path which correctly does `deltaVi.multiply(BigInteger.valueOf(userVote)).divide(DECIMAL_OF_VI_REWARD)` (multiply-then-divide with exact `BigInteger` arithmetic): [4](#0-3) 

This confirms the codebase itself recognizes multiply-before-divide as the correct approach elsewhere (and the project has an entire hardening effort — see `RepositoryImpl.usageToBalance`, `ResourceProcessor.calculateGlobalLimitV2`, `ExchangeCapsule.transaction` — replacing double/div-before-mul math with `BigInteger` exact math), but this particular `computeReward(long, List)` overload used for legacy-cycle vote rewards was not hardened and remains division-before-multiplication.

### Impact Explanation
`computeReward(long cycle, votes)` is reachable by any voter account through a single signed `WithdrawBalanceContract` transaction:
- `WithdrawBalanceActuator.execute()` calls `mortgageService.withdrawReward(ownerAddress)` [5](#0-4) 
- `withdrawReward()` calls `computeReward(beginCycle, endCycle, accountCapsule)` for the relevant cycles [6](#0-5) 
- which, for cycles prior to the new-reward-algorithm activation, dispatches into the vulnerable `computeReward(cycle, votes)` via `getOldReward`.

Because `reward += voteRate * totalReward` truncates fractional vote-share before scaling, and `reward` is accumulated as a `long`, the SR's total reward pool (`totalReward`, tracked exactly per-witness in `DelegationStore`) will not be fully distributable across all voters — the sum of `computeReward` results across all voters of an SR for a cycle can be strictly less than `totalReward`, and the shortfall is not stored anywhere or redistributed. This constitutes a permanent freezing/loss of a fraction of the legitimately earned reward for every affected voter/cycle combination where the old algorithm applies, satisfying "permanent freezing of unclaimed royalties/rewards" analogous to the original report's impact.

### Likelihood Explanation
Likelihood is limited by the fact that this only affects cycles before `NEW_REWARD_ALGORITHM_EFFECTIVE_CYCLE` was activated (a one-time chain parameter switch that appears to already be active on mainnet given `RewardViCalService`/`allowOldRewardOpt` machinery). For any chain/testnet/private network that has not yet activated the new reward algorithm, or during any period before activation, every voter's `WithdrawBalanceContract` transaction that covers pre-switch cycles deterministically triggers this precision-losing path — no special privileges, timing tricks, or malicious actors required; it is a straightforward unprivileged transaction (`WithdrawBalanceContract`) executed on ordinary voting activity.

### Recommendation
Replace the double-based `voteRate` computation in `MortgageService.computeReward(long, List<Pair<byte[], Long>>)` with exact integer arithmetic that multiplies before dividing, e.g.:

```java
long userVote = vote.getValue();
reward += BigInteger.valueOf(userVote)
    .multiply(BigInteger.valueOf(totalReward))
    .divide(BigInteger.valueOf(totalVote))
    .longValueExact();
```

This mirrors the already-hardened `deltaVi.multiply(BigInteger.valueOf(userVote)).divide(DECIMAL_OF_VI_REWARD)` pattern used in the new-algorithm path in the same file, eliminating the precision loss while preserving overflow safety.

### Proof of Concept
Given (values chosen to demonstrate truncation, mirroring the external report's method of hand-computing "correct" vs. "buggy" results):
- `totalReward = 100_000_026` (sun)
- `totalVote = 3_000_000_027` (sum of many voters' votes, an odd/non-power-of-two number causing `userVote/totalVote` to be non-terminating in binary/double precision — same class of scenario shown in `DelegationServiceTest.testPay`, which itself computes expected values using `double d = (double) 16000000 / tmp; long expect = (long) (d * 100000026);` at [7](#0-6) 
  demonstrating the test suite already relies on/models the exact same double-based (division-then-multiplication) arithmetic that the actual production code performs)
- `userVote = 16_000_000`

Buggy computation (current code):
```
voteRate = (double)16000000 / 3000000027   // truncated double, e.g. 0.005333333...
reward = (long)(voteRate * 100000026)      // further truncation at final cast
```
Correct computation (multiply-first):
```
reward = (16000000L * 100000026L) / 3000000027L   // exact integer result via BigInteger
```
For large `totalVote`/`totalReward` combinations chosen so that `userVote * totalReward` is not evenly divisible and where double precision (53-bit mantissa) cannot exactly represent the intermediate ratio, the two results diverge by 1 or more sun per voter per cycle. Since this computation runs once per voter per legacy cycle across the whole chain history, the aggregate underpayment (permanently unclaimable, as `DelegationStore`'s per-cycle `totalReward` is fixed and not adjusted for rounding) can be non-trivial at scale.

*Note: I could not confirm from the index whether `NEW_REWARD_ALGORITHM_EFFECTIVE_CYCLE` has already been permanently activated on the live mainnet (which would make this path effectively dead code today) or whether it remains reachable on any currently running network/testnet — this is uncertain without deployment/chain-state access, and would need to be verified in a full Devin session with access to the running chain configuration.*

### Citations

**File:** chainbase/src/main/java/org/tron/core/service/MortgageService.java (L108-127)
```java
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

**File:** actuator/src/main/java/org/tron/core/actuator/WithdrawBalanceActuator.java (L54-55)
```java
    mortgageService.withdrawReward(withdrawBalanceContract.getOwnerAddress()
        .toByteArray());
```

**File:** framework/src/test/java/org/tron/core/services/DelegationServiceTest.java (L45-46)
```java
    double d = (double) 16000000 / tmp;
    long expect = (long) (d * 100000026);
```
