## Title
Unbounded per-cycle iteration in `MortgageService.getOldReward` reward computation - (File: chainbase/src/main/java/org/tron/core/service/MortgageService.java)

### Summary
`MortgageService.withdrawReward()` / `queryReward()`, invoked from essentially every stake-related actuator (`FreezeBalanceActuator`, `UnfreezeBalanceActuator`, `VoteWitnessActuator`, TVM freeze/vote/unfreeze processors) as well as `VoteRewardUtil` for TVM-based accounts, computes a voter's un-withdrawn reward by walking cycle-by-cycle from the account's `beginCycle` to `endCycle`, exactly the "iterate over full history to compute a value that should be O(1)" pattern described in the report for `getLockedCount`.

### Finding Description
When an account has votes and has not withdrawn its reward, `withdrawReward`/`queryReward` calls `computeReward(beginCycle, endCycle, accountCapsule)`, which for the portion of the range prior to `newRewardAlgorithmEffectiveCycle` calls `getOldReward`: [1](#0-0) 
```
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
`allowOldRewardOpt` is a governance flag that defaults to `0` (disabled) unless a committee proposal has activated it, as confirmed by the default in `reference.conf`/`CommonParameter` and the `ProposalUtil` validation logic that treats `ALLOW_OLD_REWARD_OPT` as a one-time, not-yet-guaranteed switch: [2](#0-1) [3](#0-2) 

Until this flag is enabled network-wide, `getOldReward` falls back to a per-cycle loop, where for each of the (potentially thousands of) cycles it re-invokes `computeReward(cycle, votes)`, which itself iterates over every witness the account has voted for: [4](#0-3) 

This is reachable by any unprivileged account: simply hold votes and let `beginCycle` stay old (e.g., vote once and never trigger a subsequent freeze/unfreeze/vote for a long period), then submit any single transaction that re-invokes `withdrawReward` (a `FreezeBalanceContract`, `UnfreezeBalanceContract`, `VoteWitnessContract`, or a TVM-triggered freeze/vote/unfreeze call). All of these call `mortgageService.withdrawReward(ownerAddress)` unconditionally at the start of `execute()`: [5](#0-4) [6](#0-5) 

This mirrors the reported class of bug: a value (`getLockedCount`/here, accrued reward) that should be computable in O(1) via precomputed per-cycle aggregates is instead computed by iterating "over all history" (cycles since the account's last withdrawal), and this computation is embedded in the hot path of common, cheaply-triggerable transactions.

### Impact Explanation
Where `allowOldRewardOpt` has not been activated, a dormant voting account (or one deliberately kept dormant by an attacker who votes and lets many maintenance cycles pass, e.g. across long time spans since cycles occur every 6 hours) will trigger an O(N) loop over N historical cycles times the number of witnesses voted for, each iteration performing store lookups (`getReward`, `getWitnessVote`/`getWitnessVi`). This inflates the cost of an otherwise cheap freeze/unfreeze/vote transaction and, in the worst case for a very old account with a large vote list, could push transaction processing time and energy usage significantly higher than intended, degrading validator/node throughput for that block — the same "any token operation becomes much more expensive than it should be" impact called out in the original report, and in the extreme could contribute to resource exhaustion for the processing node.

### Likelihood Explanation
Reachable from a single unprivileged, signed transaction (freeze/unfreeze/vote, or a TVM freeze/unfreeze/vote call) with no special permissions, contingent only on the network not yet having enabled `ALLOW_OLD_REWARD_OPT`. It requires no cooperation from other parties — an attacker can artificially maximize impact by voting for the maximum number of witnesses allowed and then waiting the longest possible time before triggering a withdrawal-inducing transaction.

### Recommendation
Extend the O(1) Vi-based accrual computation (`RewardViCalService`/`getWitnessVi`) unconditionally to the pre-optimization cycle range instead of gating it behind the `allowOldRewardOpt` proposal, or ensure the proposal is activated before this code path can be reached on any live network, eliminating the linear cycle-count loop from `getOldReward`.

### Proof of Concept
1. An account votes for the maximum allowed number of witnesses via `VoteWitnessContract`/`VoteWitnessActuator`.
2. The account refrains from any transaction that would invoke `withdrawReward` (i.e., does not freeze/unfreeze/vote again) for as many maintenance cycles as possible, while `dynamicPropertiesStore.allowOldRewardOpt()` remains `0`.
3. The account (or anyone forcing an actuator that touches it, e.g., another `VoteWitnessContract`) then submits a single freeze/unfreeze/vote transaction, causing `MortgageService.withdrawReward` → `computeReward` → `getOldReward` to iterate `endCycle - beginCycle` times, each iteration re-scanning the full vote list, incurring cost proportional to elapsed cycles rather than O(1).

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

**File:** chainbase/src/main/java/org/tron/core/store/DynamicPropertiesStore.java (L2877-2890)
```java
  public void saveAllowOldRewardOpt(long allowOldRewardOpt) {
    this.put(ALLOW_OLD_REWARD_OPT, new BytesCapsule(ByteArray.fromLong(allowOldRewardOpt)));
  }

  public boolean allowOldRewardOpt() {
    return getAllowOldRewardOpt() == 1L;
  }

  public long getAllowOldRewardOpt() {
    return Optional.ofNullable(getUnchecked(ALLOW_OLD_REWARD_OPT))
        .map(BytesCapsule::getData)
        .map(ByteArray::toLong)
        .orElse(CommonParameter.getInstance().getAllowOldRewardOpt());
  }
```

**File:** actuator/src/main/java/org/tron/core/utils/ProposalUtil.java (L736-754)
```java
      case ALLOW_OLD_REWARD_OPT: {
        if (!forkController.pass(ForkBlockVersionEnum.VERSION_4_7_4)) {
          throw new ContractValidateException(
              "Bad chain parameter id [ALLOW_OLD_REWARD_OPT]");
        }
        if (dynamicPropertiesStore.allowOldRewardOpt()) {
          throw new ContractValidateException(
              "[ALLOW_OLD_REWARD_OPT] has been valid, no need to propose again");
        }
        if (value != 1) {
          throw new ContractValidateException(
              "This value[ALLOW_OLD_REWARD_OPT] is only allowed to be 1");
        }
        if (!dynamicPropertiesStore.useNewRewardAlgorithm()) {
          throw new ContractValidateException(
              "[ALLOW_NEW_REWARD] or [ALLOW_TVM_VOTE] proposal must be approved "
                  + "before [ALLOW_OLD_REWARD_OPT] can be proposed");
        }
        break;
```

**File:** actuator/src/main/java/org/tron/core/actuator/UnfreezeBalanceActuator.java (L71-75)
```java
    byte[] ownerAddress = unfreezeBalanceContract.getOwnerAddress().toByteArray();

    //
    mortgageService.withdrawReward(ownerAddress);

```

**File:** actuator/src/main/java/org/tron/core/actuator/VoteWitnessActuator.java (L160-163)
```java
    //
    mortgageService.withdrawReward(ownerAddress);

    AccountCapsule accountCapsule = accountStore.get(ownerAddress);
```
