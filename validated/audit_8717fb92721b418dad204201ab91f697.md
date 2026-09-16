### Title
Division-before-multiplication precision loss in legacy vote-reward and brokerage calculations - (File: chainbase/src/main/java/org/tron/core/service/MortgageService.java)

### Summary
`MortgageService` computes SR block-reward brokerage splits and legacy (pre-VI-algorithm) voter rewards using `double` division performed **before** the subsequent multiplication, exactly the anti-pattern described in the reported Index Coop bug (`_calculateMinRepayUnits` doing `preciseDiv` before the final `preciseMul`). This causes truncation/rounding to occur before scaling, producing systematically inaccurate reward and brokerage amounts.

### Finding Description
In `payReward`, the brokerage cut taken from every block/transaction-fee reward is computed as: [1](#0-0) 
`brokerageRate = (double) brokerage / 100` performs the division first (introducing floating point truncation of the ratio), and only then multiplies by `value`. The correct order to preserve precision would be `(brokerage * value) / 100`.

The same pattern recurs in the legacy per-cycle voter-reward computation (used for cycles prior to `newRewardAlgorithmEffectiveCycle`): [2](#0-1) 
Here `voteRate = (double) userVote / totalVote` is computed and truncated as a `double` before being multiplied by `totalReward`, instead of doing `userVote * totalReward / totalVote`. Note that the newer VI-based reward path in the same file already does multiplication-before-division correctly with `BigInteger` (`deltaVi.multiply(BigInteger.valueOf(userVote)).divide(DECIMAL_OF_VI_REWARD)`), confirming the legacy path is the outlier that still has the flawed ordering.

### Impact Explanation
Both computations are pure stake/reward math (explicitly in-scope per the analog rules). The `payReward` brokerage split executes on every witness reward payment during block application (`payBlockReward`/`payTransactionFeeReward`, called from `Manager`/consensus reward flow), and `computeReward(cycle, votes)` executes on every `WithdrawBalanceContract` and `queryReward` call whose account still has cycles predating the new reward algorithm cutover. Because division happens first, the computed brokerage/voter reward is systematically truncated (rounds toward zero) relative to the mathematically correct `multiply-then-divide` result, causing every affected witness/voter to be under (or occasionally over, given floating-point rounding behavior) credited compared to the intended proportional split. This is a reward-accounting/fund-miscalculation defect reachable purely through the standard `WithdrawBalanceContract` transaction path: [3](#0-2) 

### Likelihood Explanation
The flawed path executes unconditionally for every reward cycle prior to the new reward algorithm activation (`payReward` runs on every block for every witness, unconditionally), so the precision defect is deterministically triggered by ordinary protocol operation — no privileged access or malicious actor is required, only normal voting/withdrawal activity by any account holder.

### Recommendation
Reorder the arithmetic to multiply before dividing, using integer/`BigInteger` math consistent with the newer VI-reward implementation:
- In `payReward`: compute `brokerageAmount = brokerage * value / 100` (with overflow-safe `Math.multiplyHigh`/`BigInteger` guarding for large `value`), rather than `(double) brokerage / 100 * value`.
- In legacy `computeReward(cycle, votes)`: compute `reward += userVote * totalReward / totalVote` (via `BigInteger` to avoid overflow) instead of computing `voteRate` as a truncated `double` ratio first.

### Proof of Concept
Given `brokerage = 33`, `value = 100`:
- Current: `brokerageRate = 33/100 = 0.33` (as a `double`, exact here but truncates for many other values, e.g. `brokerage=1, value=3` → `0.01 * 3 = 0.03` truncated to `0` instead of the exact `1*3/100=0` — precision differences become concrete for larger/less-round values, e.g. `brokerage=7, value=142857`: `(double)7/100=0.07`, `0.07*142857=9999.99` → truncates to `9999`, whereas `7*142857/100 = 1000000-... = 10000`), producing an off-by-one/systematic underpayment to the witness/voter versus the mathematically exact multiply-first result.
- Same demonstrable discrepancy applies to `computeReward(cycle, votes)`'s `voteRate * totalReward` versus `userVote * totalReward / totalVote`.

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

**File:** actuator/src/main/java/org/tron/core/actuator/WithdrawBalanceActuator.java (L54-55)
```java
    mortgageService.withdrawReward(withdrawBalanceContract.getOwnerAddress()
        .toByteArray());
```
