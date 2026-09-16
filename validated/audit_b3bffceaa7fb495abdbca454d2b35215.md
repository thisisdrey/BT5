### Title
Unsafe truncating `BigInteger.longValue()` cast in witness-vote reward (Vi) accumulation and payout calculation - (File: `chainbase/src/main/java/org/tron/core/service/MortgageService.java`)

### Summary
The reward-per-vote ("Vi") accounting used to pay out delegation/voting rewards accumulates values in `BigInteger` (to avoid intermediate overflow) but then converts the final result back to a `long` using the unchecked, silently-truncating `BigInteger.longValue()` — exactly the unsafe-cast pattern flagged in the external report (`int256(pointsPerShare)` truncating/overflowing a wide correction value). The same unsafe cast is duplicated in three reachable reward-computation paths.

### Finding Description
`MortgageService.computeReward` accumulates `deltaVi.multiply(BigInteger.valueOf(userVote)).divide(DelegationStore.DECIMAL_OF_VI_REWARD).longValue()` into a `long reward` accumulator: [1](#0-0) 

The identical unsafe pattern is duplicated in the TVM-vote reward-withdrawal path reachable by any contract call: [2](#0-1) 

and in the background Vi-recalculation service: [3](#0-2) 

`DECIMAL_OF_VI_REWARD` is a fixed 10^18 scale factor, and `Vi` (`DelegationStore.getWitnessVi` / `setWitnessVi`) is a monotonically-accumulated `BigInteger` per witness per cycle: [4](#0-3) [5](#0-4) 

`Vi` for a cycle is computed as `reward * DECIMAL_OF_VI_REWARD / voteCount` and is never bounded — if `voteCount` for a witness is very small in some cycle (e.g. a witness with a tiny amount of frozen/voted TRX Power), `deltaVi` for that cycle can be made arbitrarily large by an attacker who controls that witness's vote total. Because `Vi` accumulates cycle over cycle without any upper bound check, an attacker can engineer a witness with an extremely small vote total for one or more cycles, then withdraw against a much larger `userVote` later: `deltaVi.multiply(userVote)` intentionally becomes a huge `BigInteger`, and the unsafe `.longValue()` truncation silently wraps this value into an arbitrary (including negative) 64-bit `long`, rather than throwing or clamping like `Math.toIntExact`/`longValueExact` would.

This mirrors the Sherlock finding precisely: a wide, unchecked value (`pointsPerShare` there, `deltaVi * userVote / 1e18` here) is unsafely cast down to a fixed-width signed type (`int256` there, Java `long` here) with no overflow guard, so the stored/paid reward can silently become an unintended (and possibly negative or wrapped) value instead of throwing or being rejected.

### Impact Explanation
An attacker who can influence a witness's per-cycle vote total (by voting/unvoting through ordinary `VoteWitnessContract`/`FreezeBalanceV2`/`UnfreezeBalanceV2` transactions or via the TVM `vote` precompile) can manufacture out-of-range `Vi` deltas that, when multiplied by a subsequently large vote and unsafely truncated to `long` in `MortgageService.adjustAllowance`/`accountCapsule.setAllowance`, either mint an attacker-favorable/unbacked allowance or corrupt reward accounting network-wide, leading to unbacked balance credited to an account (theft of protocol reward funds) or reward-ledger corruption. Because the result feeds directly into `AccountCapsule.setAllowance` and ultimately account balance via `WithdrawBalanceContract`, this is a direct on-chain funds-accounting bug, not merely a display issue.

### Likelihood Explanation
Reachable by any unprivileged account through ordinary signed vote/freeze/unfreeze transactions or the TVM `vote`-related precompiles — no special privilege (SR, committee, or node operator) is required. Triggering the extreme ratio needed for practical overflow requires deliberately voting with a very small amount for one or more cycles and later reconciling with a larger vote, which is entirely achievable with ordinary transactions over multiple maintenance cycles.

### Recommendation
Replace unchecked `BigInteger.longValue()` truncation in `MortgageService.computeReward`, `VoteRewardUtil.computeReward`, and `RewardViCalService.getNewRewardAlgorithmReward`/`accumulateWitnessVi` with `longValueExact()` (throwing on overflow) or an explicit bounds check (`compareTo(BigInteger.valueOf(Long.MAX_VALUE))`) before narrowing, and add a sanity cap on `Vi` growth (e.g., reject/clip cycles where `voteCount` is implausibly small relative to total network vote) so a single low-vote witness cycle cannot be used to inflate `deltaVi` unboundedly.

### Proof of Concept
1. Attacker freezes a minimal amount of TRX and votes for witness `W` such that `W`'s total cycle vote count is extremely small (e.g., attacker is the sole voter with `voteCount = 1`).
2. Over one or more maintenance cycles, `DelegationStore.accumulateWitnessVi`/`RewardViCalService.accumulateWitnessVi` computes `deltaVi = reward * 1e18 / 1`, producing an enormous `BigInteger` stored as `Vi` for that witness/cycle: [6](#0-5) 
3. Attacker then increases vote for `W` to a large amount and, at `withdrawReward`/`queryReward` time, `computeReward` executes `deltaVi.multiply(BigInteger.valueOf(userVote)).divide(DECIMAL_OF_VI_REWARD).longValue()`, where the pre-division intermediate/quotient can exceed `Long.MAX_VALUE` and silently truncate/wrap to an attacker-favorable or negative `long`, corrupting `adjustAllowance` and the resulting account balance: [7](#0-6)

### Citations

**File:** chainbase/src/main/java/org/tron/core/service/MortgageService.java (L215-241)
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

  public void adjustAllowance(byte[] address, long amount) {
    try {
      if (amount <= 0) {
        return;
      }
      adjustAllowance(accountStore, address, amount);
    } catch (BalanceInsufficientException e) {
      logger.error("WithdrawReward error: {}.", e.getMessage());
    }
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/utils/VoteRewardUtil.java (L90-110)
```java
  private static long computeReward(long beginCycle, long endCycle,
                                    AccountCapsule accountCapsule, Repository repository) {
    if (beginCycle >= endCycle) {
      return 0;
    }

    long reward = 0;
    for (Protocol.Vote vote : accountCapsule.getVotesList()) {
      byte[] srAddress = vote.getVoteAddress().toByteArray();
      BigInteger beginVi = repository.getDelegationStore().getWitnessVi(beginCycle - 1, srAddress);
      BigInteger endVi = repository.getDelegationStore().getWitnessVi(endCycle - 1, srAddress);
      BigInteger deltaVi = endVi.subtract(beginVi);
      if (deltaVi.signum() <= 0) {
        continue;
      }
      long userVote = vote.getVoteCount();
      reward += deltaVi.multiply(BigInteger.valueOf(userVote))
          .divide(DelegationStore.DECIMAL_OF_VI_REWARD).longValue();
    }
    return reward;
  }
```

**File:** chainbase/src/main/java/org/tron/core/service/RewardViCalService.java (L143-171)
```java
  public long getNewRewardAlgorithmReward(long beginCycle, long endCycle,
                                          List<Pair<byte[], Long>> votes) {
    if (!isDone()) {
      logger.warn("rewardViCalService is not done, wait for it");
      try {
        lock.await();
      } catch (InterruptedException e) {
        Thread.currentThread().interrupt();
        throw new TronDBException(e);
      }
    }

    long reward = 0;
    if (beginCycle < endCycle) {
      for (Pair<byte[], Long> vote : votes) {
        byte[] srAddress = vote.getKey();
        BigInteger beginVi = getWitnessVi(beginCycle - 1, srAddress);
        BigInteger endVi = getWitnessVi(endCycle - 1, srAddress);
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

```

**File:** chainbase/src/main/java/org/tron/core/store/DelegationStore.java (L20-22)
```java
  public static final long REMARK = -1L;
  public static final int DEFAULT_BROKERAGE = 20;
  public static final BigInteger DECIMAL_OF_VI_REWARD = BigInteger.valueOf(10).pow(18);
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
