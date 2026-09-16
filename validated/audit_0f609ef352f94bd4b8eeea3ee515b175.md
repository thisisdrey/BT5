### Title
Silent Integer Overflow in Delegation Reward Computation via `BigInteger.longValue()` - (File: `chainbase/src/main/java/org/tron/core/service/MortgageService.java`)

### Summary
`MortgageService.computeReward`, `VoteRewardUtil.computeReward`, and `RewardViCalService.getNewRewardAlgorithmReward` all compute a voter's share of stake rewards by multiplying an accumulated, unbounded `BigInteger` "value-index" (`deltaVi`) by the voter's `long userVote`, dividing by the `DECIMAL_OF_VI_REWARD` scaling constant (`10^18`), and then converting the result with `.longValue()` instead of `.longValueExact()`.

### Finding Description
The reward-per-share ("Vi") accumulator is stored as an unbounded `BigInteger` and grows every maintenance cycle without any cap: [1](#0-0) [2](#0-1) 

When a user later withdraws/queries their reward, the delta between two Vi checkpoints is multiplied by the user's raw vote count and only then converted back to a `long` using the non-exact conversion: [3](#0-2) [4](#0-3) [5](#0-4) 

`BigInteger.longValue()` silently discards the high-order bits instead of throwing when the value does not fit in a `long` — this is exactly the "lack of overflow protection" bug class described in the external report (unchecked multiplication feeding a fixed-width integer). Notably, the same codebase has an established, documented "hardened" pattern elsewhere that uses `longValueExact()` specifically to *detect* rather than silently swallow this class of overflow: [6](#0-5) [7](#0-6) 

The three reward-computation call sites above are the outliers that were never hardened this way, even though `deltaVi` is an unbounded accumulator (it is only ever added to, never capped) and `userVote` can be as large as a user's total frozen/staked TRX (bounded only by total TRX supply, ~10^17 sun). If a witness accumulates a very large Vi delta relative to its total vote weight (e.g., a witness with very low total vote count receiving rewards over many un-withdrawn cycles), the intermediate `BigInteger` product `deltaVi * userVote` can exceed `Long.MAX_VALUE` even after the `/10^18` division, and `.longValue()` will wrap it into an arbitrary (including negative) 64-bit value instead of raising an exception.

### Impact Explanation
A wrapped/truncated reward value directly corrupts `allowance` accounting for the affected account via `adjustAllowance`: [8](#0-7) 
This can result in an account receiving a wildly incorrect (potentially far too large, effectively "unbacked") allowance credit, or a negative wraparound that could underflow `AccountCapsule.allowance` when later withdrawn. Because `withdrawReward`/`queryReward` are reachable from ordinary `WithdrawBalanceContract` transactions and from `VoteRewardUtil.withdrawReward` invoked by any TVM contract using the native `vote`/`withdrawReward` precompile, this is reachable by an unprivileged transaction broadcaster or contract deployer, matching the "unbacked balance" impact category.

### Likelihood Explanation
Exploitation requires engineering a scenario where a witness's per-cycle reward-per-vote ratio (`reward / totalVote`) is extreme (e.g., voting for a witness with a very small total vote count) and letting the delta accumulate across many un-withdrawn cycles, since `deltaVi` is unbounded `BigInteger` accretion. This is achievable by an ordinary account (freeze TRX, vote for a low-vote SR, wait/accumulate cycles, then withdraw), but requires deliberate setup and many cycles, so likelihood is Medium rather than trivial/High.

### Recommendation
Replace `.longValue()` with `.longValueExact()` (or explicit bounds checking similar to `ResourceProcessor.calculateGlobalLimitV2` / `PrecompiledContracts.ModExp`) in:
- `MortgageService.computeReward(long, long, AccountCapsule)`
- `VoteRewardUtil.computeReward`
- `RewardViCalService.getNewRewardAlgorithmReward`

so that an out-of-range reward computation throws `ArithmeticException`/is rejected rather than silently wrapping into an incorrect value.

### Proof of Concept
1. Freeze a small amount of TRX and vote for a specific SR with an extremely small total vote count (or vote right as the sole voter for a witness).
2. Let block/transaction-fee rewards accumulate to that SR over many maintenance cycles without withdrawing (`payBlockReward`/`payTransactionFeeReward` → `delegationStore.addReward` → `accumulateWitnessVi`), causing `deltaVi` for that witness to grow disproportionately relative to `userVote`.
3. Call `WithdrawBalanceContract` (or the TVM `withdrawReward` native contract) to trigger `computeReward`; if `deltaVi.multiply(BigInteger.valueOf(userVote)).divide(DECIMAL_OF_VI_REWARD)` exceeds `Long.MAX_VALUE`, `.longValue()` truncates silently to an incorrect/wrapped value that is then credited via `adjustAllowance`, instead of the transaction being rejected.

### Citations

**File:** chainbase/src/main/java/org/tron/core/service/RewardViCalService.java (L155-168)
```java
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
```

**File:** chainbase/src/main/java/org/tron/core/service/RewardViCalService.java (L223-228)
```java
    } else { // Accumulate delta vi
      BigInteger deltaVi = BigInteger.valueOf(reward)
          .multiply(DECIMAL_OF_VI_REWARD)
          .divide(BigInteger.valueOf(voteCount));
      setWitnessVi(cycle, address, preVi.add(deltaVi));
    }
```

**File:** chainbase/src/main/java/org/tron/core/store/DelegationStore.java (L20-22)
```java
  public static final long REMARK = -1L;
  public static final int DEFAULT_BROKERAGE = 20;
  public static final BigInteger DECIMAL_OF_VI_REWARD = BigInteger.valueOf(10).pow(18);
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

**File:** chainbase/src/main/java/org/tron/core/service/MortgageService.java (L243-258)
```java
  public void adjustAllowance(AccountStore accountStore, byte[] accountAddress, long amount)
      throws BalanceInsufficientException {
    AccountCapsule account = accountStore.getUnchecked(accountAddress);
    long allowance = account.getAllowance();
    if (amount == 0) {
      return;
    }

    if (amount < 0 && allowance < -amount) {
      throw new BalanceInsufficientException(
          String.format("%s insufficient balance, amount: %d, allowance: %d",
              StringUtil.createReadableString(accountAddress), amount, allowance));
    }
    account.setAllowance(allowance + amount);
    accountStore.put(account.createDbKey(), account);
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/utils/VoteRewardUtil.java (L96-108)
```java
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
```

**File:** chainbase/src/main/java/org/tron/core/db/ResourceProcessor.java (L359-378)
```java
  /**
   * Hardened replacement of legacy V2 formula
   * {@code (long)(((double) frozeBalance / TRX_PRECISION)
   *               * ((double) totalLimit / totalWeight))}.
   *
   * <p>Preserves V2 semantics: equivalent to
   * {@code (frozeBalance * totalLimit) / (TRX_PRECISION * totalWeight)} with
   * a single integer truncation at the end. Critically, fractional weight
   * (i.e. {@code frozeBalance < TRX_PRECISION}) is preserved through the
   * multiplication and only truncated at the final divide, so small balances
   * yield the same proportional result as the double-arithmetic path.
   */
  protected long calculateGlobalLimitV2(long frozeBalance,
      long totalLimit, long totalWeight) {
    return BigInteger.valueOf(frozeBalance)
        .multiply(BigInteger.valueOf(totalLimit))
        .divide(BigInteger.valueOf(TRX_PRECISION)
            .multiply(BigInteger.valueOf(totalWeight)))
        .longValueExact();
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L676-684)
```java

      // use big numbers to stay safe in case of overflow
      BigInteger energy = BigInteger.valueOf(multComplexity)
          .multiply(BigInteger.valueOf(max(adjExpLen, 1, VMConfig.disableJavaLangMath())))
          .divide(GQUAD_DIVISOR);

      return isLessThan(energy, BigInteger.valueOf(Long.MAX_VALUE)) ? energy.longValueExact()
          : Long.MAX_VALUE;
    }
```
