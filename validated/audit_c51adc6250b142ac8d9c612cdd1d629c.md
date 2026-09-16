## Finding: Accumulated Rounding Loss in Vote-Reward Distribution (Vi-based Reward Algorithm)

### Title
Repeated Integer-Division Truncation in Vote Reward Calculation Causes Systemic Under-Distribution of Staking Rewards - (File: `chainbase/src/main/java/org/tron/core/service/MortgageService.java`)

### Summary
The "new reward algorithm" (Vi-based) that computes a voter's share of a Super Representative's (SR) reward pool truncates on every single computation via `BigInteger` integer division, exactly the same class of bug as the referenced `_sharesToTokens`/`_tokensToShares` rounding-down issue. Because this truncation happens independently for every voter, every SR they voted for, and every reward-withdrawal call, the fractional remainders are never credited to anyone and are permanently lost from circulation.

### Finding Description
The core computation is:
```java
reward += deltaVi.multiply(BigInteger.valueOf(userVote))
    .divide(DelegationStore.DECIMAL_OF_VI_REWARD).longValue();
``` [1](#0-0) 

This appears identically in three places that are all reachable from ordinary, unprivileged user transactions:
- `MortgageService.computeReward(beginCycle, endCycle, accountCapsule)`, invoked from `MortgageService.withdrawReward` / `queryReward`, which back the `withdrawReward` actuator and `GetReward`/`GetRewardInfo` RPC/HTTP endpoints. [2](#0-1) 
- `VoteRewardUtil.computeReward`, used by the TVM `withdrawReward` opcode path (`WithdrawRewardProcessor.execute` → `VoteRewardUtil.withdrawReward`), reachable from any contract that calls the native `withdrawReward()` precompile/opcode during a signed transaction. [3](#0-2) [4](#0-3) 
- `RewardViCalService.getNewRewardAlgorithmReward`, used for the pre-checkpoint historical reward computation. [5](#0-4) 

`BigInteger.divide` truncates toward zero, so for every `(voter, SR, cycle-range)` triple the fractional remainder below `DECIMAL_OF_VI_REWARD` (10^18) is silently discarded — it is not carried over to the next computation, not credited to the voter, not credited to the SR, and not accumulated anywhere retrievable. This is functionally identical to the reported `_sharesToTokens`/`_tokensToShares` pattern: fixed-point conversion via a constant divider (`DECIMAL_OF_VI_REWARD` here plays the role of `DIVIDER`), rounding down on every call, with no compensating remainder tracking.

The same rounding pattern also exists in the pre-2019 "old" algorithm which additionally mixes in floating point:
```java
double voteRate = (double) userVote / totalVote;
reward += voteRate * totalReward;
``` [6](#0-5) 
and in the brokerage-fee split, which is computed with `double` arithmetic and floor-cast to `long` on every single block/transaction-fee reward payment:
```java
double brokerageRate = (double) brokerage / 100;
long brokerageAmount = (long) (brokerageRate * value);
``` [7](#0-6) 
This function, `payReward`, runs on every block produced and every transaction-fee reward paid to every SR on the network — i.e., it executes continuously as part of ordinary chain operation triggered by broadcast transactions, so any truncation error compounds across millions of calls over the life of the chain.

### Impact Explanation
Every reward computation rounds strictly downward and the truncated remainder is never redistributed to the voter, the SR, or any recoverable pool — it disappears from the accounting entirely. Because `computeReward`/`getNewRewardAlgorithmReward` are invoked per-vote, per-SR, per-cycle-span, and `payReward` runs on every block reward and every transaction fee reward across the whole network, the aggregate amount of permanently unallocated TRX grows continuously and is not recoverable by any code path — it is not credited to the SR's reward pool, the voter's allowance, or the account balance. This matches the "permanent freezing of funds" impact category: value that should have been distributed as staking rewards is permanently stuck/lost due to the deterministic truncation behavior, and it happens automatically for every ordinary reward-withdrawal transaction with no privileged access required.

### Likelihood Explanation
This is not a rare edge case — it triggers on essentially every `withdrawReward` call (via `WithdrawRewardActuator`, the TVM `withdrawReward` precompile, or `GetReward`/`queryReward` RPC calls) and every block/transaction-fee reward payout (`payBlockReward`/`payTransactionFeeReward` called from `Manager` block application). Any account that votes for an SR and later withdraws rewards, and every SR receiving block/fee rewards, is affected on essentially every cycle, making the likelihood of the discrepancy occurring effectively 100% under normal network operation.

### Recommendation
Use higher-precision fixed-point representation (e.g., keep an explicit remainder tracked per voter/SR to be carried into subsequent reward computations, similar to a "dust accumulator" pattern), or perform reward distribution with `RoundingMode.HALF_UP`/banker's rounding combined with remainder carry-forward so that cumulative reward payouts converge to the true total reward pool over time rather than monotonically leaking value through floor-only truncation. Replace the `double`-based brokerage rate computation in `payReward` with integer/`BigDecimal` fixed-point math to avoid floating-point precision loss compounding over every block.

### Proof of Concept
1. An SR accumulates a reward of `R` for a cycle, split among `N` voters proportionally to their vote weight, using `deltaVi.multiply(userVote).divide(DECIMAL_OF_VI_REWARD)`.
2. For any voter whose `deltaVi * userVote` is not an exact multiple of `DECIMAL_OF_VI_REWARD` (10^18), the fractional remainder (up to `DECIMAL_OF_VI_REWARD - 1` in Vi-units, translating to sub-sun/small TRX amounts) is truncated and lost on every single `withdrawReward` call.
3. Because this computation re-executes independently for each `(voter, SR, cycle range)` combination on every withdrawal, and `payReward`'s brokerage split executes on every block, the total unrecoverable, permanently-lost amount grows monotonically across the chain's lifetime with no mechanism in `MortgageService`, `VoteRewardUtil`, or `RewardViCalService` to reclaim or redistribute it.

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

**File:** chainbase/src/main/java/org/tron/core/service/MortgageService.java (L89-89)
```java
  public void withdrawReward(byte[] address) {
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
