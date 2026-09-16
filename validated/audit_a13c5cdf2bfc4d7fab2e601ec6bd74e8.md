### Title
Duplicated reward-computation logic causes `WithdrawRewardOnChain` opcode to miscompute vote rewards for cycles before the new algorithm - ([File: actuator/src/main/java/org/tron/core/vm/utils/VoteRewardUtil.java])

### Summary
Vote-reward calculation exists in two independent, hand-duplicated implementations: `MortgageService.computeReward` (used by the regular `WithdrawBalanceActuator`/`UnfreezeBalanceActuator` path) and `VoteRewardUtil.computeReward` (used by the TVM `WithdrawRewardProcessor`, reachable from any smart contract via the `WithdrawRewardOnChain`-style opcode). The two implementations have diverged: `MortgageService` branches on the "new reward algorithm effective cycle" and falls back to the legacy per-cycle reward algorithm (`getOldReward`/`computeReward(cycle, votes)`) for cycles prior to that cutover, while `VoteRewardUtil.computeReward` always applies only the new Vi-delta algorithm for the entire `[beginCycle, endCycle)` range, with no legacy fallback.

### Finding Description
`MortgageService.computeReward(long beginCycle, long endCycle, AccountCapsule accountCapsule)` [1](#0-0)  splits the reward window at `dynamicPropertiesStore.getNewRewardAlgorithmEffectiveCycle()`: cycles before the cutover use `getOldReward` (legacy per-witness/per-cycle reward pool division), and only cycles at/after the cutover use the Vi-delta algorithm.

`VoteRewardUtil.computeReward(long beginCycle, long endCycle, AccountCapsule accountCapsule, Repository repository)` [2](#0-1)  is a hand-copied duplicate that omits the `newAlgorithmCycle` branch entirely and always computes reward purely from `getWitnessVi` deltas for the whole cycle range, regardless of whether those cycles precede the new-algorithm cutover.

Because `RewardViStore` (`rewardViStore`) accumulation for pre-cutover cycles is not guaranteed to reflect the legacy reward-pool semantics (the Vi accumulation is explicitly gated behind `RewardViCalService`, which only starts computing from `newRewardCalStartCycle` onward), any account whose vote reward window spans cycles before the new algorithm's effective cycle will get a different reward when withdrawn via the TVM native `WithdrawRewardOnChain` path (`WithdrawRewardProcessor.execute` → `VoteRewardUtil.withdrawReward`) [3](#0-2)  than via the normal `WithdrawBalanceActuator`/`MortgageService.withdrawReward` path [4](#0-3) .

This is precisely the "error-prone code duplication" pattern from the external report (duplicated business logic with subtly different semantics that can silently diverge) — here the duplication is not merely stylistic but produces two different reward numbers for the same account/cycle-range depending on which entry point (regular transaction vs. smart-contract opcode) is used.

### Impact Explanation
An account withdrawing vote rewards through a smart contract call (TVM `WithdrawRewardOnChain`) instead of the `WithdrawBalanceContract` transaction can receive a different (potentially larger) `allowance` credited to its balance than the canonical `MortgageService` computation would produce for the same cycle window, because the pre-cutover legacy reward pool division is skipped in favor of Vi-delta math that was not designed to be valid for those cycles. Depending on the state of `rewardViStore` for pre-cutover cycles (i.e., whether `RewardViCalService.startRewardCal()` backfilled consistent values), this can result in an over- or under-payment of TRX allowance, i.e., an unbacked balance credited to the calling account — reachable by any address that owns votes and calls a contract that invokes the withdraw-reward native contract.

### Likelihood Explanation
Any account can trigger this by deploying/calling a trivial contract that invokes the TVM `WithdrawRewardOnChain` native contract instead of sending a normal `WithdrawBalanceContract` transaction — this requires no special privilege, only that the account has pending vote rewards spanning cycles around the `NEW_REWARD_ALGORITHM_EFFECTIVE_CYCLE` transition. Because the divergence is a pure logic bug in duplicated code (not a race condition or multi-step attack), it is deterministically reproducible whenever the reward window crosses the old/new-algorithm boundary.

### Recommendation
Eliminate the duplicated reward-computation logic: have `VoteRewardUtil.computeReward` delegate to (or share) the exact same code path as `MortgageService.computeReward`, including the `newAlgorithmCycle` split and `getOldReward` fallback, ideally by having `WithdrawRewardProcessor`/`VoteRewardUtil` call into `MortgageService` (or a shared helper) rather than reimplementing the algorithm against `Repository`. Add regression tests that withdraw rewards via both the actuator path and the TVM native-contract path for accounts whose vote windows straddle the new-algorithm cutover cycle, asserting identical payouts.

### Proof of Concept
1. Advance the chain until `NEW_REWARD_ALGORITHM_EFFECTIVE_CYCLE` is set to some cycle `N` (via the normal upgrade proposal process already present in the codebase).
2. Create an account that voted for a witness and accrues reward across cycles that span both sides of `N` (i.e., `beginCycle < N < endCycle`).
3. Path A: Submit a `WithdrawBalanceContract` transaction — reward is computed via `MortgageService.computeReward`, using `getOldReward` for the pre-`N` portion.
4. Path B: Deploy/call a contract that invokes the TVM native `WithdrawRewardOnChain` contract for the same account/cycle window — reward is computed via `VoteRewardUtil.computeReward`, which always uses the Vi-delta algorithm for the whole range.
5. Compare the two resulting `allowance`/balance credits for equivalent cycle windows (using two otherwise-identical test accounts/vote histories) — they diverge, demonstrating the duplication bug produces inconsistent, exploitable reward accounting depending on which entry point is used.

### Citations

**File:** chainbase/src/main/java/org/tron/core/service/MortgageService.java (L89-134)
```java
  public void withdrawReward(byte[] address) {
    if (!dynamicPropertiesStore.allowChangeDelegation()) {
      return;
    }
    AccountCapsule accountCapsule = accountStore.get(address);
    long beginCycle = delegationStore.getBeginCycle(address);
    long endCycle = delegationStore.getEndCycle(address);
    long currentCycle = dynamicPropertiesStore.getCurrentCycleNumber();
    long reward = 0;
    if (beginCycle > currentCycle || accountCapsule == null) {
      return;
    }
    if (beginCycle == currentCycle) {
      AccountCapsule account = delegationStore.getAccountVote(beginCycle, address);
      if (account != null) {
        return;
      }
    }
    //withdraw the latest cycle reward
    if (beginCycle + 1 == endCycle && beginCycle < currentCycle) {
      AccountCapsule account = delegationStore.getAccountVote(beginCycle, address);
      if (account != null) {
        reward = computeReward(beginCycle, endCycle, account);
        adjustAllowance(address, reward);
        reward = 0;
        logger.info("Latest cycle reward {}, {}.", beginCycle, account.getVotesList());
      }
      beginCycle += 1;
    }
    //
    endCycle = currentCycle;
    if (CollectionUtils.isEmpty(accountCapsule.getVotesList())) {
      delegationStore.setBeginCycle(address, endCycle + 1);
      return;
    }
    if (beginCycle < endCycle) {
      reward += computeReward(beginCycle, endCycle, accountCapsule);
      adjustAllowance(address, reward);
    }
    delegationStore.setBeginCycle(address, endCycle);
    delegationStore.setEndCycle(address, endCycle + 1);
    delegationStore.setAccountVote(endCycle, address, accountCapsule);
    logger.info("Adjust {} allowance {}, now currentCycle {}, beginCycle {}, endCycle {}, "
            + "account vote {}.", Hex.toHexString(address), reward, currentCycle,
        beginCycle, endCycle, accountCapsule.getVotesList());
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

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/WithdrawRewardProcessor.java (L38-41)
```java
  public long execute(WithdrawRewardParam param, Repository repo) throws ContractExeException {
    byte[] ownerAddress = param.getOwnerAddress();

    VoteRewardUtil.withdrawReward(ownerAddress, repo);
```
