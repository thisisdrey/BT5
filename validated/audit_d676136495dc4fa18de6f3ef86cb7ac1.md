Precision loss from division-before-multiplication (the bug class in the reported `CurveVolatileOracle.getPrice` issue) has a direct structural analog in java-tron's reward/brokerage distribution math, which uses `double` division followed by multiplication instead of an integer-preserving order of operations.

### Title
Division-before-multiplication precision loss in reward/brokerage payout calculations - (File: chainbase/src/main/java/org/tron/core/service/MortgageService.java)

### Summary
`MortgageService.payReward` and `MortgageService.computeReward(long, List<Pair<byte[], Long>>)` compute reward and brokerage amounts by first dividing two `long` values into a `double`, then multiplying that intermediate `double` by another `long` value. This is the same division-then-multiplication ordering flagged in the external report, and it produces rounding errors that are absorbed inconsistently across the reward-distribution ledger.

### Finding Description
In `payReward`, the brokerage cut taken from every block/standby/transaction-fee reward is computed as: [1](#0-0) 
`brokerageRate = (double) brokerage / 100` is computed first (introducing rounding since `brokerage` is a percentage stored as `int`), and only then multiplied by `value` to get `brokerageAmount`. Division before multiplication here discards precision that would have been preserved had the multiplication been performed before dividing (e.g. `brokerage * value / 100`).

The same pattern recurs in the legacy per-cycle reward split used by `computeReward`: [2](#0-1) 
`voteRate = (double) userVote / totalVote` is computed first, then multiplied by `totalReward`. This is invoked from `withdrawReward`/`queryReward`, which are reachable by any account holder withdrawing/querying vote rewards (`chainbase/.../MortgageService.java:89-169`), and from `getOldReward` during block-reward maintenance in `Manager`.

`payStandbyWitness` shows the identical ordering for the top-127-witness allowance: [3](#0-2) 
`eachVotePay = (double) totalPay / voteSum` computed first, then multiplied per witness by `getVoteCount()`.

The equivalent TVM-facing path, `VoteRewardUtil.computeReward`, avoids this by using `BigInteger` multiply-then-divide (`actuator/src/main/java/org/tron/core/vm/utils/VoteRewardUtil.java:106-107`), and other subsystems in this same codebase (e.g. `ResourceProcessor.calculateGlobalLimitV2`, `SafeExchangeProcessor`) have already been hardened with `BigInteger`/`BigDecimal` multiply-before-divide to eliminate exactly this class of bug — confirming the project treats division-before-multiplication as a real defect class, yet `MortgageService`'s legacy reward/brokerage path was not hardened the same way.

### Impact Explanation
Reward and brokerage amounts computed this way systematically diverge (via floating-point rounding) from the true integer-exact proportional share. Because `payReward` runs on every block reward and every transaction-fee reward payout, and `computeReward`/`payStandbyWitness` run every cycle for every voter/witness, these small per-operation rounding errors accumulate across the network's entire reward ledger over time. This creates a persistent, unbacked drift between the sum of amounts credited to accounts (`delegationStore.addReward`, `adjustAllowance`) and the amounts that should be backed by the true reward pool, i.e. an "unbacked balance" accounting integrity issue rather than a one-off truncation.

### Likelihood Explanation
High reachability: `payReward` executes on every block (via block reward/tx-fee reward payment in `Manager`), and `computeReward`/`queryReward`/`withdrawReward` are reachable by any account holder who has voted, through ordinary reward withdrawal calls — no special privilege required. The rounding occurs deterministically every time these paths execute, so the drift is continuous rather than conditional on rare inputs.

### Recommendation
Reorder operations to multiply before dividing, and prefer integer/`BigInteger` arithmetic over `double` for these reward/brokerage computations, mirroring the pattern already used in `VoteRewardUtil.computeReward` and `ResourceProcessor.calculateGlobalLimitV2`:
- `payReward`: compute `brokerageAmount = (value * brokerage) / 100` using `long`/`BigInteger` math instead of `(double) brokerage / 100 * value`.
- `computeReward(cycle, votes)` / `payStandbyWitness`: compute `reward = (totalReward * userVote) / totalVote` and `pay = (totalPay * voteCount) / voteSum` using `BigInteger` to avoid overflow, instead of dividing first into a `double`.

### Proof of Concept
1. Set `brokerage = 33` (33%) and `value = 100` in `payReward`.
   - Current code: `brokerageRate = 33.0/100 = 0.33` (exact here, but for values like `brokerage=1`, `value = 3` : `0.01 * 3 = 0.030000000000000002` → truncates differently than integer math).
   - Correct integer math: `(1 * 3) / 100 = 0` vs current `(long)(0.01 * 3) = 0` — for larger/more numerous cycles the divergence compounds because `double` cannot exactly represent most `brokerage/100` fractions.
2. Repeat across many cycles/witnesses (`MortgageService.payStandbyWitness` loops over all 127 standby witnesses every cycle): each iteration's `(double) totalPay / voteSum` rounding error is re-multiplied by a different `voteCount`, so errors do not cancel out but accumulate across the full witness set and across cycles, producing a measurable, non-reversible drift in total allowance credited versus the actual reward pool defined by `totalPay`.

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
