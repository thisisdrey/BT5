### Title
Legacy vote-reward calculation in `MortgageService.computeReward()` uses floating-point division-before-multiplication, causing voters to receive fewer rewards than owed - (File: chainbase/src/main/java/org/tron/core/service/MortgageService.java)

### Summary
`WithdrawBalanceActuator`, an actuator executable from any signed `WithdrawBalanceContract` transaction, calls `MortgageService.withdrawReward()`, which for cycles predating `newRewardAlgorithmEffectiveCycle` computes rewards via the legacy `computeReward(long cycle, List<Pair<byte[], Long>> votes)` path. That method performs the division `userVote / totalVote` before multiplying by `totalReward`, using `double` arithmetic, exactly the "division before multiplication" pattern flagged in the external report for `Infiltration.sol::escape()`.

### Finding Description
`WithdrawBalanceActuator.execute()` unconditionally calls `mortgageService.withdrawReward(ownerAddress)` [1](#0-0) , which in turn calls `computeReward(beginCycle, endCycle, accountCapsule)` [2](#0-1) .

For any cycle range that begins before `dynamicPropertiesStore.getNewRewardAlgorithmEffectiveCycle()`, the reward is computed by the legacy path `getOldReward(...)` which in turn calls the per-cycle `computeReward(long cycle, List<Pair<byte[], Long>> votes)`:
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
``` [3](#0-2) 

This computes `userVote / totalVote` first (producing a lossy `double` ratio), then multiplies by `totalReward`, instead of `userVote * totalReward / totalVote`. This is mathematically identical to the `_escapeMultiplier()` bug pattern in the external report: performing division before multiplication introduces rounding/precision loss that is compounded across every voter and every affected cycle, biased toward under-payment.

Note that in the newer path (cycles at/after the algorithm cutover), the codebase correctly performs multiply-before-divide using `BigInteger` (`deltaVi.multiply(BigInteger.valueOf(userVote)).divide(DECIMAL_OF_VI_REWARD)`) [4](#0-3) , confirming the legacy `computeReward(long cycle, ...)` method is the outlier still using the flawed division-first, floating-point pattern.

### Impact Explanation
Any voter with un-withdrawn rewards accrued in cycles prior to the new-reward-algorithm cutover, who calls `WithdrawBalanceContract` (a permissionless, self-signed transaction any account can broadcast), receives a reward computed via lossy floating-point division-before-multiplication rather than exact integer multiply-then-divide. This systematically underpays legitimate reward claims — a concrete case of funds a user is entitled to being silently reduced, matching the "theft/permanent freezing of funds via unbacked/undercalculated balance" impact category. Because `double` precision is used (not integer truncation with a controlled floor), the error is compounded with `totalReward`'s magnitude and accumulated per-vote per-cycle, and it benefits no other identifiable party (the value effectively evaporates from the reward pool accounting), rather than being distributed correctly.

### Likelihood Explanation
The legacy code path is only reachable when `beginCycle < newRewardAlgorithmEffectiveCycle`, i.e., for reward-withdrawal transactions covering a cycle range that predates the network's one-time reward-algorithm migration. Any account that voted and did not withdraw before the cutover, or that spans across the cutover boundary, will exercise this exact computation when calling `WithdrawBalanceContract` today, so the code path remains live and reachable via a single, ordinary, unprivileged transaction; it is not a purely historical/dead branch since `beginCycle`/`endCycle` bookkeeping is per-account and can still reference pre-cutover cycles for accounts that have not withdrawn since.

### Recommendation
Rewrite `computeReward(long cycle, List<Pair<byte[], Long>> votes)` to avoid floating point and division-before-multiplication, e.g.:
```java
reward += (userVote * totalReward) / totalVote; // using BigInteger/Math.multiplyHigh to avoid overflow
```
using `BigInteger` (or `Math.multiplyExact`/`Math.floorDiv` with overflow-safe widening) exactly as already done in the newer Vi-based reward path in the same class, so multiplication happens before division and no floating-point rounding is introduced.

### Proof of Concept
1. Attacker (or any regular voter) votes for a witness in a cycle `C` that is before `newRewardAlgorithmEffectiveCycle`, and does not withdraw immediately.
2. The account's `beginCycle`/`endCycle` window spans cycle `C`; when the account eventually submits a `WithdrawBalanceContract` transaction, `WithdrawBalanceActuator.execute()` invokes `mortgageService.withdrawReward(address)` [1](#0-0) .
3. Internally this calls `computeReward(beginCycle, endCycle, accountCapsule)` → `getOldReward(...)` → `computeReward(cycle, votes)`, which computes `voteRate = (double) userVote / totalVote; reward += voteRate * totalReward;` [5](#0-4) .
4. For values where `userVote * totalReward` is not evenly divisible by `totalVote` (the common case), the double-precision division-then-multiply yields a value strictly ≤ the exact `userVote * totalReward / totalVote`, so the credited `allowance` in `WithdrawBalanceActuator` is less than the mathematically owed reward — the difference is unrecoverable by the user.

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
