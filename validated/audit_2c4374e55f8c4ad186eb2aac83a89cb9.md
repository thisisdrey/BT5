## Title
Division-before-multiplication in legacy vote-reward computation causes reward pot precision loss / potential over-issuance - (File: `chainbase/src/main/java/org/tron/core/service/MortgageService.java`)

### Summary
`MortgageService.computeReward(long cycle, List<Pair<byte[], Long>> votes)` (the "old algorithm" reward path) computes a voter's share of a witness's fixed reward pot by first **dividing** `userVote / totalVote` into a `double`, and only afterwards **multiplying** by `totalReward`: [1](#0-0) 

This is the same bug class as the reported IchiLpOracle issue: division is performed before the multiplying factor is applied, discarding precision that the multiplication could have preserved (e.g. `BigInteger`/scaled integer multiply-then-divide, as done correctly elsewhere in this same codebase, see `VoteRewardUtil.computeReward` and the new-algorithm path in `MortgageService.computeReward(long, long, AccountCapsule)`, which multiply by `BigInteger` first then divide once at the end): [2](#0-1) 

### Finding Description
For cycles before `getNewRewardAlgorithmEffectiveCycle()`, `computeReward` uses `double voteRate = (double) userVote / totalVote; reward += voteRate * totalReward;`. `userVote`/`totalVote`/`totalReward` are `long` sun-denominated quantities that can reach into the 10^16–10^17 range for TRX vote counts on a live chain, exceeding the ~15-16 significant decimal digits a `double` can represent exactly. Dividing first collapses the ratio into a lossy floating-point value before it is ever multiplied by `totalReward`, so:
- Small individual truncation/rounding errors are introduced per voter.
- Because `totalReward` (the fixed reward pot for a witness/cycle, set once via `delegationStore.addReward` in `MortgageService.payReward`) is shared by an unbounded number of voters, the accumulated floating-point rounding across all voters is not guaranteed to sum to `totalReward`. Depending on rounding direction it can systematically **exceed** the backing pot (creating TRX not backed by the recorded reward) or **under-pay** voters (silently burning legitimate rewards), unlike the multiply-first/divide-last integer approach used by the newer algorithm and by `VoteRewardUtil`.

This is reachable by any unprivileged account: `computeReward(cycle, votes)` is invoked from `MortgageService.withdrawReward`/`queryReward`, which are called directly from ordinary, permissionless actuators triggered by a single signed transaction: [3](#0-2) 
as well as `VoteWitnessActuator`, `UnfreezeBalanceActuator`, and `UnfreezeBalanceV2Actuator` (all call `mortgageService.withdrawReward`).

### Impact Explanation
Because reward computation happens per-account on withdrawal rather than as one atomic distribution, rounding drift accumulated with `double` arithmetic can cause the sum of amounts credited to `accountCapsule.setAllowance(...)` across all voters of a witness/cycle to diverge from the fixed `totalReward` pot recorded by `payReward`. In the worst case this manifests as unbacked TRX balance being created for voters (funds minted beyond what was actually paid into the reward pot for that witness/cycle), which matches the "unbacked balance" impact category. At minimum it causes voters to be systematically under/over paid relative to their true voting share, a fairness/precision-loss defect directly analogous to the reported LP oracle undervaluation.

### Likelihood Explanation
This code path only executes for cycles prior to `newRewardAlgorithmEffectiveCycle` (a legacy/pre-upgrade code path). On networks that have already passed that boundary in production this is dormant, but the vulnerable code remains live and reachable in any deployment/testnet/private chain where the new-reward-algorithm activation cycle has not yet occurred, and it is triggered by the completely ordinary, permissionless `WithdrawBalanceContract`/`VoteWitnessContract`/`UnfreezeBalanceContract` transactions — no special privilege is required, only having cast votes in an eligible old cycle.

### Recommendation
Replace the `double`-based `voteRate` computation in `MortgageService.computeReward(long, List<Pair<byte[], Long>>)` with integer/`BigInteger` multiply-then-divide arithmetic, mirroring the pattern already used in `VoteRewardUtil.computeReward` and the new-algorithm branch of `MortgageService.computeReward(long, long, AccountCapsule)`: compute `reward += (BigInteger.valueOf(totalReward).multiply(BigInteger.valueOf(userVote))).divide(BigInteger.valueOf(totalVote))` so precision is preserved until the single final integer division.

### Proof of Concept
1. Construct (in a test / private network) a witness `sr` with recorded old-algorithm reward `totalReward = X` for cycle `c` (`delegationStore.addReward(c, sr, X)`), and `totalVote = T` for that witness.
2. Have many voter accounts each cast a non-uniform `userVote_i` such that `sum(userVote_i) == T`, chosen so each `(double) userVote_i / T` cannot be represented exactly (e.g. values yielding repeating binary fractions).
3. Each voter calls `WithdrawBalanceContract` (or `VoteWitnessActuator`/`UnfreezeBalanceActuator`, which also invoke `mortgageService.withdrawReward`) to trigger `computeReward` for cycle `c`.
4. Sum all `accountCapsule.getAllowance()` values credited across the voters and compare to `X`; due to independent double rounding per voter this sum can differ from `X`, demonstrating the precision-loss/over- or under-issuance analogous to the reported LP oracle bug.

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

**File:** actuator/src/main/java/org/tron/core/actuator/WithdrawBalanceActuator.java (L54-55)
```java
    mortgageService.withdrawReward(withdrawBalanceContract.getOwnerAddress()
        .toByteArray());
```
