Based on my research, I found a strong analog to the reported Morpho bug class in java-tron's reward-withdrawal logic, where a **mandatory, forced iteration** (analogous to Morpho's forced `_defaultIterations.withdraw`/`repay` loop) grows unbounded with elapsed time and can cause an OOG revert that permanently blocks a user's ability to unfreeze/withdraw funds.

### Title
Unbounded per-cycle reward computation loop in `MortgageService.withdrawReward` / `VoteRewardUtil.withdrawReward` can OOG-revert mandatory unfreeze/withdraw/vote operations, permanently freezing staked funds - (File: `chainbase/src/main/java/org/tron/core/service/MortgageService.java`)

### Summary
`withdrawReward()` is invoked unconditionally (not opt-in) at the start of `UnfreezeBalanceActuator`, `UnfreezeBalanceV2Actuator`, `WithdrawBalanceActuator`, and `VoteWitnessActuator`, mirroring Morpho's pattern where the protocol forces a bounded-looking loop into operations the user cannot skip. Unlike Morpho's `maxIterations`, this loop's iteration count is not capped by any protocol parameter — it scales with `endCycle - beginCycle`, i.e., the number of maintenance cycles elapsed since the account's last reward withdrawal, which is entirely a function of wall-clock time, not a value any actuator caps.

### Finding Description
`MortgageService.withdrawReward` (and its VM-native twin `VoteRewardUtil.withdrawReward`) computes an account's reward via `computeReward(beginCycle, endCycle, accountCapsule)`. [1](#0-0) 

For cycles prior to `newRewardAlgorithmEffectiveCycle`, this delegates to `getOldReward`, which — unless `allowOldRewardOpt` is enabled (an on-chain committee-controlled parameter, off by default until proposed and activated) — loops once per cycle between `begin` and `end`, each iteration doing `computeReward(cycle, votes)` (itself O(votes) store reads): [2](#0-1) 

`computeReward(cycle, votes)` per legacy cycle does a `DelegationStore` reward/vote lookup for every one of the account's votes: [3](#0-2) 

Because `beginCycle`/`endCycle` tracking is only advanced when `withdrawReward` actually executes, an account that votes once (up to `MAX_VOTE_NUMBER` witnesses, validated in `VoteWitnessActuator.validate`) and then never interacts again accumulates an ever-growing cycle gap — bounded only by wall-clock time, with no cap enforced anywhere in the actuator or contract validation layer. [4](#0-3) 

This mirrors the Morpho pattern exactly: the loop bound (`_defaultIterations` there, `endCycle - beginCycle` here) is not something the caller can reduce, and it sits directly in the mandatory path of `UnfreezeBalanceActuator`, `UnfreezeBalanceV2Actuator`, and `WithdrawBalanceActuator` — the TRON equivalents of Morpho's forced withdraw/repay: [5](#0-4) [6](#0-5) 

### Impact Explanation
If a voting account's accumulated legacy-cycle gap grows large enough that `getOldReward`'s per-cycle loop exceeds the energy available in a single transaction/block, every subsequent call to `withdrawReward` for that account reverts with OOG. Since `withdrawReward` executes unconditionally at the top of `UnfreezeBalanceActuator`/`UnfreezeBalanceV2Actuator`/`WithdrawBalanceActuator`, the account becomes permanently unable to unfreeze its staked TRX or withdraw its vote reward allowance — a permanent freezing-of-funds condition, since there is no code path to unfreeze/withdraw that bypasses `withdrawReward`.

### Likelihood Explanation
Triggering this requires no malicious actor, only the passage of time on a chain where `allowOldRewardOpt` (and by extension the `RewardViCalService` optimization) has not been activated via committee proposal for the relevant historical cycle range, combined with any account that votes and then stays dormant for a long stretch. This is a plausible operational scenario for real staking accounts, not a contrived attack, making it a realistic Medium-severity risk analogous to Morpho's OOG concern for withdraw/repay.

### Recommendation
Cap the maximum number of legacy cycles processed per `withdrawReward` call (analogous to stress-testing and bounding `_defaultIterations`), or force `allowOldRewardOpt`/the Vi-based O(1)-per-range algorithm to be the only code path, removing the per-cycle legacy loop entirely so reward computation cost no longer scales with elapsed dormancy time.

### Proof of Concept
1. Deploy/observe an account that calls `VoteWitnessActuator` once, casting votes for witnesses.
2. Ensure the network has not enabled `allowOldRewardOpt` for the cycle range in question (default/un-proposed state).
3. Let a large number of maintenance cycles elapse without the account calling any action that triggers `withdrawReward` (i.e., don't unfreeze, withdraw, or re-vote).
4. Attempt `UnfreezeBalanceActuator`/`UnfreezeBalanceV2Actuator`/`WithdrawBalanceActuator` for that account — `withdrawReward` → `getOldReward` iterates `endCycle - beginCycle` times, each doing `MAX_VOTE_NUMBER`-bounded store reads; once the cycle gap is large enough this exceeds the transaction's energy limit and reverts, and will continue to revert on every retry since the state (`beginCycle`) is never advanced on a reverted transaction.

### Citations

**File:** chainbase/src/main/java/org/tron/core/service/MortgageService.java (L89-133)
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

**File:** chainbase/src/main/java/org/tron/core/service/MortgageService.java (L260-269)
```java
  private long getOldReward(long begin, long end, List<Pair<byte[], Long>> votes) {
    if (dynamicPropertiesStore.allowOldRewardOpt()) {
      return rewardViCalService.getNewRewardAlgorithmReward(begin, end, votes);
    }
    long reward = 0;
    for (long cycle = begin; cycle < end; cycle++) {
      reward += computeReward(cycle, votes);
    }
    return reward;
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/VoteWitnessActuator.java (L93-97)
```java
    int maxVoteNumber = MAX_VOTE_NUMBER;
    if (contract.getVotesCount() > maxVoteNumber) {
      throw new ContractValidateException(
          "VoteNumber more than maxVoteNumber " + maxVoteNumber);
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/UnfreezeBalanceActuator.java (L71-76)
```java
    byte[] ownerAddress = unfreezeBalanceContract.getOwnerAddress().toByteArray();

    //
    mortgageService.withdrawReward(ownerAddress);

    AccountCapsule accountCapsule = accountStore.get(ownerAddress);
```

**File:** actuator/src/main/java/org/tron/core/actuator/WithdrawBalanceActuator.java (L53-59)
```java

    mortgageService.withdrawReward(withdrawBalanceContract.getOwnerAddress()
        .toByteArray());

    AccountCapsule accountCapsule = accountStore.
        get(withdrawBalanceContract.getOwnerAddress().toByteArray());
    long oldBalance = accountCapsule.getBalance();
```
