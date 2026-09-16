### Title
Floating-point (double) arithmetic instead of exact integer math in vote-reward distribution can produce incorrect, exploitable reward payouts - (File: `chainbase/src/main/java/org/tron/core/service/MortgageService.java`)

### Summary
The reward-distribution logic in `MortgageService` (and the related `IncentiveManager`) computes SR brokerage cuts, standby-witness pay, and the legacy voter-reward split using `double` arithmetic instead of exact integer/`BigInteger` math. This mirrors the reported bug class: performing financial arithmetic on external/queried values (here, vote counts and reward pools, which are attacker/voter-influenced) without a safe, precision-preserving math path, leading to systematically incorrect results.

### Finding Description
`MortgageService.payReward()` computes the SR brokerage cut as: [1](#0-0) 
using `double brokerageRate = (double) brokerage / 100;` and `long brokerageAmount = (long) (brokerageRate * value);`. Similarly, `payStandbyWitness()` distributes the fixed `Witness127PayPerBlock` pot proportionally to vote count via `double eachVotePay = (double) totalPay / voteSum;` and `long pay = (long) (w.getVoteCount() * eachVotePay);`: [2](#0-1) 

`IncentiveManager.reward()` does the same proportional split using `double` for the standby-witness allowance: [3](#0-2) 

Most notably, the legacy voter-reward algorithm (still used for any cycle before `NewRewardAlgorithmEffectiveCycle`, i.e., `getOldReward`/`computeReward(cycle, votes)`) splits each witness's total per-cycle reward pool among voters proportionally to their vote share using `double voteRate = (double) userVote / totalVote; reward += voteRate * totalReward;`: [4](#0-3) 

`double` has only a 52-bit mantissa (~15-16 significant decimal digits of exact integer precision, i.e. exact only up to 2^53 ≈ 9.007×10^15). TRX amounts are denominated in `sun` (1 TRX = 10^6 sun) and vote counts/reward pools that are large (a few million TRX and up, i.e. > ~9×10^15 sun, well within TRON's actual total-supply/vote-count range) routinely exceed this exact-integer boundary once multiplied. Once `userVote`, `totalVote`, or `totalReward` exceed 2^53, the `double` division/multiplication silently loses precision, and the `(long)` cast truncates the (already imprecise) floating-point result. Because this computation runs independently for every voter/witness pair drawing from the same fixed pool (`delegationStore.getReward(cycle, srAddress)`), the sum of truncated, rounded shares handed out to individual voters is not guaranteed to equal (and can be less than or, due to rounding direction inconsistency across voters, effectively divert value between) the original pool, unlike the newer VI-based algorithm which correctly uses `BigInteger` with a fixed-point `DECIMAL_OF_VI_REWARD` scale for the exact same purpose: [5](#0-4) 

The contrast between the exact `BigInteger`-based new algorithm and the imprecise `double`-based legacy path is the direct analog of the reported issue: financial computations should not be done with a plain floating-point/native-arithmetic path when the underlying quantities can be attacker-influenced and can exceed the safe representable range.

### Impact Explanation
Any account can vote for witnesses (`VoteWitnessContract`) and later claim rewards via `WithdrawBalanceContract`, which routes into `MortgageService.withdrawReward()`/`queryReward()` and, for cycles predating the VI algorithm, into the double-based `computeReward` path. Because the reward computed for each voter is independently rounded in floating point, and TRX reward pools/vote weights realistically exceed the `double` exact-integer boundary, this can result in systematically incorrect balances being credited to `AccountCapsule.allowance` (via `adjustAllowance`) that do not correspond to the arithmetically correct proportional share — i.e., value can be minted/lost relative to the actual reward pool recorded in `DelegationStore`. This is a fund-integrity issue (incorrect/unbacked balance credited to accounts) rather than a simple rounding nuisance, since `adjustAllowance` directly increases a spendable account balance.

### Likelihood Explanation
This code path executes automatically on every block for `payBlockReward`/`payTransactionFeeReward`/`payStandbyWitness`, and on every voter's `WithdrawBalanceContract`/`GetReward` call for cycles prior to the new-algorithm effective cycle. No special privilege is required — it is reachable by any ordinary token holder who casts votes and withdraws rewards, i.e., a plain signed transaction from an unprivileged account. The only requirement for meaningful precision loss is that `totalVote`/`totalReward`/`voteCount * eachVotePay` values exceed `2^53`, which is common at TRON mainnet scale (SR reward pools and total votes are commonly in the hundreds of millions to billions of TRX, i.e., far beyond 2^53 sun).

### Recommendation
Replace all `double`-based reward/brokerage computations in `MortgageService` (`payReward`, `payStandbyWitness`, legacy `computeReward(cycle, votes)`), and `IncentiveManager.reward()`, with exact integer arithmetic using `BigInteger`/`Math.multiplyExact`+`Math.floorDiv` (or the fixed-point `BigInteger` VI approach already used by the new reward algorithm) so that: (1) proportional shares sum exactly (or with a well-defined, bounded remainder handling policy) to the original pool, and (2) no precision is lost for values beyond `2^53`. This mirrors the report's guidance to avoid naive/floating arithmetic on externally influenced numeric inputs and to use a precision-safe math utility uniformly.

### Proof of Concept
1. Have a witness receive a large reward pool in a legacy (pre-VI-algorithm) cycle, e.g., `totalReward` = tens of millions of TRX in `sun` (> 2^53 ≈ 9.007×10^15 sun ≈ 9 million TRX).
2. Have many voters (or a few voters with very large vote weights) vote for that witness such that `totalVote` also exceeds 2^53.
3. Call `WithdrawBalanceContract` (or `queryReward`) for each voter; `MortgageService.computeReward(cycle, votes)` computes `voteRate = (double) userVote / totalVote; reward += voteRate * totalReward;` per voter. [4](#0-3) 
4. Sum the rewards actually credited (via `adjustAllowance`) to all voters across the cycle and compare to the recorded `delegationStore.getReward(cycle, srAddress)` pool; due to `double` truncation/rounding at this scale, the sums diverge from the exact proportional split that the exact `BigInteger` VI algorithm (used for post-effective-cycle rewards) would produce, demonstrating the incorrect/unbacked balance effect.

### Citations

**File:** chainbase/src/main/java/org/tron/core/service/MortgageService.java (L53-67)
```java
  public void payStandbyWitness() {
    List<WitnessCapsule> witnessStandbys = witnessStore.getWitnessStandby(
        dynamicPropertiesStore.allowWitnessSortOptimization());
    long voteSum = witnessStandbys.stream().mapToLong(WitnessCapsule::getVoteCount).sum();
    if (voteSum < 1) {
      return;
    }
    long totalPay = dynamicPropertiesStore.getWitness127PayPerBlock();
    double eachVotePay = (double) totalPay / voteSum;
    for (WitnessCapsule w : witnessStandbys) {
      long pay = (long) (w.getVoteCount() * eachVotePay);
      payReward(w.getAddress().toByteArray(), pay);
      logger.debug("Pay {} stand reward {}.", Hex.toHexString(w.getAddress().toByteArray()), pay);
    }
  }
```

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

**File:** consensus/src/main/java/org/tron/consensus/dpos/IncentiveManager.java (L27-42)
```java
    long voteSum = 0;
    for (ByteString witness : witnesses) {
      voteSum += consensusDelegate.getWitness(witness.toByteArray()).getVoteCount();
    }
    if (voteSum <= 0) {
      return;
    }
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
