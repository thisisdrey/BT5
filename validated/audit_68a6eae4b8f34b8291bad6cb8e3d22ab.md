This is not exploitable in the same way as the report—`checkUnfreezeBalance` in `UnfreezeBalanceV2Actuator.java` and `UnfreezeBalanceV2Processor.java` strictly validates `unfreezeBalance <= frozenBalance`, so unstaking more than the frozen balance is rejected. The stronger analog is the reward-per-vote (`Vi`) accounting in `MortgageService`/`VoteRewardUtil`/`RewardViCalService`, which mirrors the report's "sum of balances ≠ total supply after rewards" bug class.

### Title
Reward accounting rounding in vote-reward "Vi" accumulator can permanently trap TRX allowance dust - ([File: chainbase/src/main/java/org/tron/core/service/RewardViCalService.java])

### Summary
The vote reward accounting system in java-tron (`MortgageService`, `VoteRewardUtil`, `RewardViCalService`, `DelegationStore`) distributes per-cycle witness rewards to voters using a cumulative "value-per-vote" (`Vi`) index computed with `BigInteger` floor division, and, for pre-optimization cycles, a `double`-based rate computation. Both mechanisms are structurally identical to the reported StWSX.sol bug: a fixed reward pool (`totalReward`, analogous to "total supply") is divided among voters proportionally to their vote weight (analogous to per-user "balances"), and the division/truncation steps cause the sum of amounts users can actually claim to diverge from the pool that was credited into `DelegationStore`.

### Finding Description
`DelegationStore.accumulateWitnessVi()` / `RewardViCalService.accumulateWitnessVi()` compute a per-cycle delta index as: [1](#0-0) 
`deltaVi = reward * DECIMAL_OF_VI_REWARD / voteCount` (BigInteger floor division), which is stored cumulatively per witness per cycle.

When a voter withdraws, `MortgageService.computeReward(beginCycle, endCycle, accountCapsule)` (and its identical TVM-facing twin `VoteRewardUtil.computeReward`) recomputes the voter's share as: [2](#0-1) 
`reward += deltaVi * userVote / DECIMAL_OF_VI_REWARD` — a second floor division. Because `deltaVi` was itself already floored when computed from `reward/voteCount`, and is floored again when multiplied back by an individual `userVote` and divided by the fixed-point scale, the amount a single voter can claim from the pool systematically differs (by design, downward) from what would be an exact proportional split.

For cycles prior to the new algorithm's effective cycle, the legacy path `MortgageService.computeReward(long cycle, List<Pair<byte[], Long>> votes)` uses `double` arithmetic: [3](#0-2) 
`voteRate = (double) userVote / totalVote; reward += voteRate * totalReward;` where the compound assignment silently narrows the double result into the `long reward` field on every loop iteration. Summed across all voters who split a given witness's `totalReward` pool for a cycle, per-voter roundings do not reconcile to exactly `totalReward` credited via `DelegationStore.addReward()` and later paid out through `MortgageService.adjustAllowance()` → `WithdrawRewardProcessor.execute()` (which adds `allowance` directly onto real TRX `balance`): [4](#0-3) 

This is directly analogous to StWSX's "sum of balances doesn't match total supply after `oracleClaimRewards()`" — here the "total supply" is the reward amount actually deposited into `DelegationStore` via `payReward()` and the "balances" are the sums voters can independently claim via `queryReward()`/`withdrawReward()`.

### Impact Explanation
Because every division here floors (never rounds up), the divergence is one-directional: some fraction of each witness's reward pool becomes permanently unclaimable dust — it is credited into `DelegationStore` per cycle but no combination of individual voter withdrawals can ever sum to exactly that amount, and there is no sweep/reclaim mechanism for the residual. This is a form of permanent, small-value fund freezing embedded in the core staking-reward accounting, reachable simply by any account calling `FreezeBalanceV2`/`VoteWitness`/`WithdrawBalance`-style transactions — no privileged role required. It does not, by itself, allow theft or overdraft beyond the pool because truncation is floor-only in the paths reviewed.

### Likelihood Explanation
Every voter interaction with the DPoS voting/reward system — which is exercised on effectively every cycle, by every voting account, through ordinary signed transactions (`WithdrawBalanceContract`, `UnfreezeBalanceV2Contract`, TVM `withdrawReward`/`voteWitness` precompiles) — triggers this computation path, so the discrepancy accumulates continuously across the network as deposits, votes and rewards increase, exactly mirroring the audit's observation that "this difference is currently small but could potentially increase as more users deposit and receive rewards."

### Recommendation
Track cumulative distributed amounts per witness per cycle (or globally) against the recorded `totalReward`, and reconcile/sweep residual dust similarly to the "dust rebase" mitigation the original report's team adopted, or switch to higher-precision fixed-point accounting (e.g., retain remainders across cycles in `DelegationStore`/`RewardViCalService` instead of discarding them at each floor division).

### Proof of Concept
Conceptual trace (no code changes needed to observe):
1. A witness accrues `totalReward = R` for cycle `c` via `MortgageService.payReward()` → `DelegationStore.addReward(c, witness, R)`.
2. `RewardViCalService`/`DelegationStore.accumulateWitnessVi()` computes `deltaVi = floor(R * 10^18 / voteCount)`.
3. Each of `N` voters with `userVote_i` calls withdraw; each receives `floor(deltaVi * userVote_i / 10^18)`.
4. `sum(floor(deltaVi * userVote_i / 10^18)) for i in 1..N` is generally `< R` (never `> R` in the paths reviewed), leaving `R - sum(...)` permanently stranded in `DelegationStore` with no code path to claim or reclaim it — repeating every cycle across every witness compounds this residual over the chain's lifetime. [5](#0-4) [6](#0-5)

### Citations

**File:** chainbase/src/main/java/org/tron/core/store/DelegationStore.java (L35-53)
```java
  public void addReward(long cycle, byte[] address, long value) {
    byte[] key = buildRewardKey(cycle, address);
    BytesCapsule bytesCapsule = get(key);
    if (bytesCapsule == null) {
      put(key, new BytesCapsule(ByteArray.fromLong(value)));
    } else {
      put(key, new BytesCapsule(ByteArray
          .fromLong(ByteArray.toLong(bytesCapsule.getData()) + value)));
    }
  }

  public long getReward(long cycle, byte[] address) {
    BytesCapsule bytesCapsule = get(buildRewardKey(cycle, address));
    if (bytesCapsule == null) {
      return 0L;
    } else {
      return ByteArray.toLong(bytesCapsule.getData());
    }
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

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/WithdrawRewardProcessor.java (L38-53)
```java
  public long execute(WithdrawRewardParam param, Repository repo) throws ContractExeException {
    byte[] ownerAddress = param.getOwnerAddress();

    VoteRewardUtil.withdrawReward(ownerAddress, repo);

    AccountCapsule accountCapsule = repo.getAccount(ownerAddress);
    long oldBalance = accountCapsule.getBalance();
    long allowance = accountCapsule.getAllowance();
    long newBalance = 0;

    try {
      newBalance = LongMath.checkedAdd(oldBalance, allowance);
    } catch (ArithmeticException e) {
      logger.debug(e.getMessage(), e);
      throw new ContractExeException(e.getMessage());
    }
```
