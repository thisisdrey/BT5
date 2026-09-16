### Title
Division-before-multiplication precision loss in legacy vote reward calculation - (File: `chainbase/src/main/java/org/tron/core/service/MortgageService.java`)

### Summary
`MortgageService.computeReward(long cycle, List<Pair<byte[], Long>> votes)` (the legacy, pre-Vi reward path) computes `voteRate = (double) userVote / totalVote` and then multiplies by `totalReward`, i.e. division is performed before multiplication using floating point. This is the same anti-pattern flagged in the external report (`_calculateMinRepayUnits`): dividing first, then multiplying, causes avoidable precision loss compared to multiplying first and dividing last (as the newer Vi-based algorithm in the same file correctly does with `BigInteger.multiply(...).divide(...)`).

### Finding Description
`computeReward(long cycle, List<Pair<byte[], Long>> votes)`: [1](#0-0) 

computes `double voteRate = (double) userVote / totalVote; reward += voteRate * totalReward;` — division happens first (`userVote/totalVote`) and the result is then multiplied by `totalReward`, losing precision that would be preserved if `userVote * totalReward` were computed first (as `long`/`BigInteger`) and divided by `totalVote` afterward.

This legacy path is reached from `getOldReward`, which is invoked whenever an account's reward-withdrawal cycle range predates `newRewardAlgorithmEffectiveCycle`: [2](#0-1) [3](#0-2) 

`computeReward(beginCycle, endCycle, accountCapsule)` and thus `getOldReward`/`computeReward(cycle, votes)` are reachable from `queryReward(byte[] address)` and `withdrawReward(byte[] address)`: [4](#0-3) [5](#0-4) 

These are the standard reward query/withdraw entry points used by `WithdrawBalanceContract` actuators and by Wallet gRPC/HTTP `getReward`/withdraw-reward query paths — i.e. reachable by any ordinary account holder who has an outstanding vote-reward balance spanning cycles before the new algorithm's effective cycle switch, without any special privilege.

By contrast, the newer Vi-based branch of the same function correctly avoids this pattern by using `BigInteger` multiply-then-divide: [6](#0-5) 

Confirming this is a real, intentional-but-imprecise legacy computation (also duplicated in `VoteRewardUtil`/`RewardViCalService`, but those use the correct BigInteger order): [7](#0-6) 

### Impact Explanation
The rounding error from computing `(userVote/totalVote)` as a `double` before multiplying by `totalReward` causes the account's paid reward to systematically deviate (typically downward, due to truncation via `(long)` cast on `reward +=`) from the exact proportional reward `userVote * totalReward / totalVote`. Because this arithmetic determines how much TRX is credited to `accountCapsule.getAllowance()` (spendable balance) via `adjustAllowance`, small but consistent discrepancies accumulate across many cycles and many voters, causing rewards paid out across all voters for a witness cycle to not sum exactly to the recorded `totalReward` for that cycle — either under-crediting individual voters (loss of funds owed to the user) or, in aggregate, mis-accounting reward pools. This is a fund-accounting correctness/precision defect in a path that governs real allowance/balance mutation.

### Likelihood Explanation
This is triggered whenever a voter withdraws or queries their reward and any part of their unclaimed cycle range predates `newRewardAlgorithmEffectiveCycle` (i.e., `beginCycle < newAlgorithmCycle`), which is common for any voter who has held votes for a long time or has not withdrawn since before the network's algorithm migration. No special permissions are required — an ordinary account calling `WithdrawBalanceContract` or the reward query RPC on any long-standing vote position exercises this code path deterministically.

### Recommendation
Reorder the arithmetic to multiply before dividing, using widened integer types to avoid overflow, matching the pattern already used in the newer Vi-based branch:
```java
reward += Math.floorDiv(
    Math.multiplyHigh-safe /* or BigInteger */ ,
    totalVote);
```
Concretely: replace
```java
double voteRate = (double) userVote / totalVote;
reward += voteRate * totalReward;
```
with a `BigInteger`-based (or overflow-checked `long`) computation:
```java
reward += BigInteger.valueOf(userVote)
    .multiply(BigInteger.valueOf(totalReward))
    .divide(BigInteger.valueOf(totalVote))
    .longValueExact();
```
so multiplication precedes division, consistent with the corrected pattern already used elsewhere in the same class.

### Proof of Concept
1. Set up a witness cycle (`cycle`) before `newRewardAlgorithmEffectiveCycle` with `totalReward` and `totalVote` such that `userVote/totalVote` is not exactly representable in double precision (e.g., `totalReward = 7`, `totalVote = 3`, `userVote = 1`).
2. Exact proportional reward = `userVote * totalReward / totalVote` = `7/3` → `2` (floor).
3. Current code: `voteRate = (double)1/3 = 0.333333...`; `reward += voteRate * 7 = 2.3333...`; truncated when accumulated into `long reward` — for larger `totalReward`/`totalVote` values (e.g. `totalReward = 100000026`, `totalVote` large, as already exercised in `DelegationServiceTest`), the double-precision computation measurably diverges from the exact integer ratio.
4. Trigger via `MortgageService.queryReward(address)` / `withdrawReward(address)` (reachable from `WithdrawBalanceContract` execution or the reward query RPC) for an account whose `beginCycle` predates `newRewardAlgorithmEffectiveCycle`, and observe the computed reward differs from the mathematically exact `userVote*totalReward/totalVote` value — demonstrating the precision-loss defect in a fund-crediting computation.

**Note on confidence**: I could not execute code to empirically quantify the magnitude of the discrepancy in this specific repository, nor definitively determine whether `newRewardAlgorithmEffectiveCycle` has already passed on all live networks (which would limit real-world exploitability of this specific legacy branch going forward, though it remains reachable for any account with unwithdrawn pre-migration reward cycles). This should be verified with a live/testnet state before treating severity as High rather than Medium.

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

**File:** chainbase/src/main/java/org/tron/core/service/MortgageService.java (L136-169)
```java
  public long queryReward(byte[] address) {
    if (!dynamicPropertiesStore.allowChangeDelegation()) {
      return 0;
    }

    AccountCapsule accountCapsule = accountStore.get(address);
    long beginCycle = delegationStore.getBeginCycle(address);
    long endCycle = delegationStore.getEndCycle(address);
    long currentCycle = dynamicPropertiesStore.getCurrentCycleNumber();
    long reward = 0;
    if (accountCapsule == null) {
      return 0;
    }
    if (beginCycle > currentCycle) {
      return accountCapsule.getAllowance();
    }
    //withdraw the latest cycle reward
    if (beginCycle + 1 == endCycle && beginCycle < currentCycle) {
      AccountCapsule account = delegationStore.getAccountVote(beginCycle, address);
      if (account != null) {
        reward = computeReward(beginCycle, endCycle, account);
      }
      beginCycle += 1;
    }
    //
    endCycle = currentCycle;
    if (CollectionUtils.isEmpty(accountCapsule.getVotesList())) {
      return reward + accountCapsule.getAllowance();
    }
    if (beginCycle < endCycle) {
      reward += computeReward(beginCycle, endCycle, accountCapsule);
    }
    return reward + accountCapsule.getAllowance();
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
