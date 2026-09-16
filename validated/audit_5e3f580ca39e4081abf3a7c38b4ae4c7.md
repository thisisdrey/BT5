Confirmed: `WithdrawBalanceActuator.execute` at [1](#0-0)  calls `mortgageService.withdrawReward(...)`, which is directly reachable from a signed `WithdrawBalanceContract` transaction broadcast by any account. That method's old-algorithm reward path performs a divide-then-multiply double-precision calculation, matching the reported bug class (precision loss from imprecise floating-point division/multiplication in a financial calculation).

### Title
Precision loss in legacy (pre-new-algorithm) witness voting reward calculation via floating-point division-then-multiplication - (File: `chainbase/src/main/java/org/tron/core/service/MortgageService.java`)

### Summary
`MortgageService.computeReward(long cycle, List<Pair<byte[], Long>> votes)`, the "old algorithm" reward-distribution routine used for cycles before `NEW_REWARD_ALGORITHM_EFFECTIVE_CYCLE`, computes a voter's share of a Super Representative's total reward using `double` arithmetic instead of integer/`BigInteger` math: [2](#0-1) . This mirrors the reported `SfrxEthEthOracle` issue class — a financial ratio computed via floating-point division and multiplication is inherently imprecise, and results are silently truncated when cast to `long`, and then accumulated across every SR a voter has voted for and every cycle in the affected range.

### Finding Description
`double voteRate = (double) userVote / totalVote; reward += voteRate * totalReward;` divides first, losing precision in the mantissa, then multiplies by `totalReward`, amplifying the rounding error, and finally truncates via implicit `(long)` cast when accumulated. All other reward-distribution paths in the same class (`payReward`, the new-algorithm `computeReward` using `BigInteger` and `DECIMAL_OF_VI_REWARD`, and `RewardViCalService`) intentionally avoid this pattern by using `BigInteger`-based fixed-point math or delayed truncation, confirming this specific branch is the outlier / legacy weak point: [3](#0-2) .

This method is reached from a normal user-broadcast transaction:
1. Any account calls `WithdrawBalanceContract` → `WithdrawBalanceActuator.execute` → `mortgageService.withdrawReward(address)` [1](#0-0) .
2. `withdrawReward` calls `computeReward(beginCycle, endCycle, accountCapsule)` for cycles spanning the account's vote history [4](#0-3) .
3. For any portion of that range prior to `newAlgorithmCycle`, `getOldReward` is invoked, which (when `allowOldRewardOpt` is not set) loops per-cycle calling the double-based `computeReward(cycle, votes)` [5](#0-4) .
4. The (imprecise, truncated) result is added directly to the account's `allowance`/balance via `adjustAllowance`, i.e., it directly determines minted TRX credited to the caller.

Because `totalVote`, `userVote`, and `totalReward` are attacker/voter-influenced (an account can choose exactly how much TRX-power to vote with, and can vote for many SRs to multiply the number of times this lossy calculation runs), the systematic rounding bias is both attacker-observable and attacker-steerable in aggregate, unlike a one-off floating point rounding.

### Impact Explanation
Repeated per-cycle, per-vote truncation from `(double)` division causes the sum of all voters' computed rewards for an SR/cycle to diverge from the SR's actual `totalReward` pool recorded in `DelegationStore`. Depending on rounding direction this can systematically under-pay honest voters (funds effectively stuck/lost, since the SR's `totalReward` bucket for that cycle is otherwise exhausted once every voter withdraws) or, in aggregate across many voters/cycles, over-issue allowance beyond what was actually reserved, i.e., unbacked balance credited to `allowance`. Given TRX-value amounts and the fact that this executes as a normal signed transaction outcome, this is a fund-accounting integrity issue in a broadcastable path.

### Likelihood Explanation
This code path only executes for cycles before `NEW_REWARD_ALGORITHM_EFFECTIVE_CYCLE` was enabled. On the current mainnet that threshold has long been passed, so exercising this exact branch today requires either a chain/environment where the new-reward-algorithm proposal was never activated (e.g., private/consortium chains, testnets that haven't enabled it) or historical unwithdrawn cycles that predate activation being processed by any account calling `WithdrawBalanceContract`/`WithdrawRewardProcessor` for old cycles that are still pending withdrawal. Where reachable, it requires no special privilege — any voter triggers it simply by withdrawing rewards.

### Recommendation
Replace the `double`-based ratio computation in `computeReward(long cycle, List<Pair<byte[], Long>> votes)` with exact integer arithmetic, e.g. `BigInteger.valueOf(totalReward).multiply(BigInteger.valueOf(userVote)).divide(BigInteger.valueOf(totalVote))`, matching the pattern already used in the new-algorithm path (`deltaVi.multiply(BigInteger.valueOf(userVote)).divide(DelegationStore.DECIMAL_OF_VI_REWARD)`), so the reward share is computed via multiply-then-divide with exact integers rather than double division followed by multiplication and implicit truncation.

### Proof of Concept
1. Configure a chain/testnet where `ALLOW_NEW_REWARD`/`NEW_REWARD_ALGORITHM_EFFECTIVE_CYCLE` has not yet been activated (or roll back to a state prior to activation, as in `DelegationServiceTest.testPay`, which itself asserts double-based expected values matching production code) [6](#0-5) .
2. Have multiple accounts vote for the same SR with vote weights chosen such that `userVote/totalVote` is not exactly representable in binary floating point (e.g., `userVote=1`, `totalVote=3`).
3. Advance a cycle so `payReward` credits `totalReward` for that SR into `DelegationStore`.
4. Each voter broadcasts a `WithdrawBalanceContract` transaction, triggering `MortgageService.withdrawReward` → `computeReward` (old algorithm).
5. Sum all voters' credited allowances for that cycle/SR and compare against the SR's recorded `totalReward`; the sums diverge due to the double-precision truncation applied per voter, demonstrating the same class of "incorrect price/ratio calculation due to precision loss" as the referenced report.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/WithdrawBalanceActuator.java (L54-55)
```java
    mortgageService.withdrawReward(withdrawBalanceContract.getOwnerAddress()
        .toByteArray());
```

**File:** chainbase/src/main/java/org/tron/core/service/MortgageService.java (L124-127)
```java
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

**File:** framework/src/test/java/org/tron/core/services/DelegationServiceTest.java (L30-56)
```java
  private void testPay(int cycle) {
    double rate = 0.2;
    if (cycle == 0) {
      rate = 0.1;
    } else if (cycle == 1) {
      rate = 0.2;
    }
    mortgageService.payStandbyWitness();
    Wallet.setAddressPreFixByte(ADD_PRE_FIX_BYTE_MAINNET);
    byte[] sr1 = decodeFromBase58Check("TLTDZBcPoJ8tZ6TTEeEqEvwYFk2wgotSfD");
    long value = dbManager.getDelegationStore().getReward(cycle, sr1);
    long tmp = 0;
    for (int i = 0; i < 27; i++) {
      tmp += 100000000 + i;
    }
    double d = (double) 16000000 / tmp;
    long expect = (long) (d * 100000026);
    long brokerageAmount = (long) (rate * expect);
    expect -= brokerageAmount;
    Assert.assertEquals(expect, value);
    mortgageService.payBlockReward(sr1, 32000000);
    expect += 32000000;
    brokerageAmount = (long) (rate * 32000000);
    expect -= brokerageAmount;
    value = dbManager.getDelegationStore().getReward(cycle, sr1);
    Assert.assertEquals(expect, value);
  }
```
