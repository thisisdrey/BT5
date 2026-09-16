### Title
Floating-point precision loss in legacy vote-reward pro-rata distribution causes unbacked/miscalculated TRX reward payouts - (File: `chainbase/src/main/java/org/tron/core/service/MortgageService.java`)

### Summary
`MortgageService` computes SR vote rewards for "old algorithm" cycles using Java `double` arithmetic to split a fixed reward pool proportionally among voters, instead of exact integer/`BigInteger` math. This mirrors the reported `rewardPerToken`-style floating-point precision defect: the pro-rata share `voteRate = (double) userVote / totalVote` and `reward += voteRate * totalReward` is only IEEE-754 approximate, so summed payouts to all voters of a given SR/cycle are not guaranteed to equal the actual funded `totalReward`, allowing systematic over- or under-payment as vote/reward magnitudes grow.

### Finding Description
`computeReward(long cycle, List<Pair<byte[], Long>> votes)` divides `userVote` by `totalVote` as a `double` and multiplies by `totalReward` (also a `long` reward pool amount), accumulating into a `long reward`: [1](#0-0) 

This routine is invoked, per-cycle, by `getOldReward`, which loops from `beginCycle` to `newAlgorithmCycle` calling `computeReward` for every intervening cycle — compounding rounding error across many cycles: [2](#0-1) 

`getOldReward`/`computeReward(cycle, votes)` is reached from the main reward-computation path `computeReward(beginCycle, endCycle, accountCapsule)`, which is itself invoked from `withdrawReward(address)` and `queryReward(address)` whenever `beginCycle < newAlgorithmCycle` (i.e., any account whose reward window still spans pre-cutover cycles): [3](#0-2) 

`withdrawReward` is directly reachable by an unprivileged, unprivileged transaction broadcaster via the `WithdrawBalanceContract` actuator, which any voter/SR account can submit: [4](#0-3) 

Because `double` has only a 53-bit mantissa (~2^53 ≈ 9.007×10^15 exact integer range), and TRON's `sun`-denominated reward pools/vote weights can approach or exceed that range (TRX max supply ×10^6 sun ≈ 10^17), the `voteRate * totalReward` computation loses precision. Since there is no invariant check that `Σ(voteRate_i * totalReward)` over all voters `i` equals `totalReward`, rounding can cause the sum of all individually computed and paid-out rewards for a cycle/SR to exceed the actual reward pool that was funded via `payReward`/`addReward`, effectively minting TRX (unbacked allowance credited via `adjustAllowance`, later converted into real balance on `WithdrawBalanceActuator.execute`). Conversely, systematic under-payment permanently strands a fraction of legitimately earned rewards, which are never re-attributed to anyone.

### Impact Explanation
`adjustAllowance` writes directly to `AccountCapsule.allowance`, which `WithdrawBalanceActuator.execute` later adds straight to `balance`: [5](#0-4) 
Any accumulated over-payment from floating-point rounding becomes real, spendable TRX balance with no corresponding debit elsewhere — an unbacked-balance condition. Under-payment permanently and silently strands funds owed to legitimate voters since there is no reconciliation mechanism for the residual.

### Likelihood Explanation
The vulnerable path only activates for accounts whose reward window (`beginCycle`) still precedes `newRewardAlgorithmEffectiveCycle`, i.e., legacy/dormant voter accounts that have not withdrawn since before the new algorithm cutover. This is a naturally, passively reachable condition — no attacker action beyond a normal `WithdrawBalanceContract` transaction (or TVM-triggered path) is required — but the magnitude of drift depends on reward/vote sizes being large enough to exceed `double`'s exact-integer precision, and on the number of dormant legacy accounts that still traverse this code path.

### Recommendation
Replace `double`-based pro-rata computation in `MortgageService.computeReward(long cycle, List<Pair<byte[], Long>> votes)` with exact `BigInteger`/`long` integer arithmetic (multiply-then-divide, as already done in the newer `BigInteger`-based Vi algorithm in `DelegationStore`/`RewardViCalService`), and enforce an invariant that the sum of rewards distributed for a cycle never exceeds the recorded `totalReward` for that cycle/witness.

### Proof of Concept
1. Fund an SR's reward pool for a legacy cycle (`delegationStore.addReward(cycle, srAddress, totalReward)`) with a large `totalReward` value close to or exceeding 2^53 sun, and record `witnessVote` similarly large.
2. Have multiple voter accounts each holding a vote share of that SR, whose `beginCycle` remains below `newRewardAlgorithmEffectiveCycle`.
3. Each voter submits a `WithdrawBalanceContract` transaction, triggering `MortgageService.withdrawReward` → `computeReward` → `getOldReward` → `computeReward(cycle, votes)`.
4. Sum the `allowance` credited to all voters for that cycle/SR and compare against the originally funded `totalReward`; due to double rounding, the sum diverges from `totalReward`, demonstrating unbacked-balance creation or permanent loss of legitimately earned rewards.

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

**File:** chainbase/src/main/java/org/tron/core/service/MortgageService.java (L199-214)
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

**File:** actuator/src/main/java/org/tron/core/actuator/WithdrawBalanceActuator.java (L42-56)
```java
    final WithdrawBalanceContract withdrawBalanceContract;
    AccountStore accountStore = chainBaseManager.getAccountStore();
    DynamicPropertiesStore dynamicStore = chainBaseManager.getDynamicPropertiesStore();
    MortgageService mortgageService = chainBaseManager.getMortgageService();
    try {
      withdrawBalanceContract = any.unpack(WithdrawBalanceContract.class);
    } catch (InvalidProtocolBufferException e) {
      logger.debug(e.getMessage(), e);
      ret.setStatus(fee, code.FAILED);
      throw new ContractExeException(e.getMessage());
    }

    mortgageService.withdrawReward(withdrawBalanceContract.getOwnerAddress()
        .toByteArray());

```

**File:** actuator/src/main/java/org/tron/core/actuator/WithdrawBalanceActuator.java (L57-68)
```java
    AccountCapsule accountCapsule = accountStore.
        get(withdrawBalanceContract.getOwnerAddress().toByteArray());
    long oldBalance = accountCapsule.getBalance();
    long allowance = accountCapsule.getAllowance();

    long now = dynamicStore.getLatestBlockHeaderTimestamp();
    accountCapsule.setInstance(accountCapsule.getInstance().toBuilder()
        .setBalance(oldBalance + allowance)
        .setAllowance(0L)
        .setLatestWithdrawTime(now)
        .build());
    accountStore.put(accountCapsule.createDbKey(), accountCapsule);
```
