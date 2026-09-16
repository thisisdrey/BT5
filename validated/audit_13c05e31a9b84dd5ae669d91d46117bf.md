### Title
Division-before-multiplication in `MortgageService.computeReward(cycle, votes)` causes voter reward truncation/precision loss - (File: chainbase/src/main/java/org/tron/core/service/MortgageService.java)

### Summary
`MortgageService.computeReward(long cycle, List<Pair<byte[], Long>> votes)`, the legacy per-cycle vote-reward calculation used before the "new reward algorithm" (Vi-based) took effect, computes `voteRate = (double) userVote / totalVote` and then multiplies that ratio by `totalReward`, i.e. it divides before multiplying. This is the same defective operation order described in the external report (`preciseMul` -> `preciseDiv` reordering causing loss of precision), just performed with `double` arithmetic instead of a fixed-point library, and it directly determines how much reward a delegator receives.

### Finding Description
In [1](#0-0) , for each vote the code does:
```
double voteRate = (double) userVote / totalVote;
reward += voteRate * totalReward;
```
Computing `userVote / totalVote` first collapses the ratio into a `double` before it is multiplied by `totalReward`. Because `userVote` and `totalVote` are both `long` values that can be large (vote counts scale with frozen TRX, up into the billions), and `totalReward` can also be large, doing the division first discards precision that a `(userVote * totalReward) / totalVote`-style computation (multiply first) would have preserved. The compound assignment `reward += voteRate * totalReward` then narrows the `double` result back to `long`, truncating any fractional remainder that was already corrupted by the earlier division. This is the identical algebraic defect flagged in the external report against `_calculateMinRepayUnits` (`x.mul(a).div(b).mul(c)` vs. doing division last) — only here it is `x.div(y) [first] .mul(z)` rather than the correct `x.mul(z).div(y)`.

This routine (`computeReward(cycle, votes)`) is invoked from `getOldReward` at [2](#0-1)  whenever `dynamicPropertiesStore.allowOldRewardOpt()` is false, which is itself invoked from the general reward path `computeReward(beginCycle, endCycle, accountCapsule)` at [3](#0-2) . That, in turn, is called by both `withdrawReward(address)` and `queryReward(address)`: [4](#0-3) [5](#0-4) 

`withdrawReward` is reachable from an unprivileged, signed transaction through `WithdrawBalanceActuator`, and `queryReward` is reachable through query paths (Wallet / TVM `VoteRewardUtil`), so any account holder who has voted for a witness in a cycle predating the new reward algorithm can trigger this miscalculation simply by withdrawing or querying their staking reward.

The same "divide-then-multiply-with-double" pattern also appears in `payStandbyWitness()` (`eachVotePay = (double) totalPay / voteSum; ... pay = (long)(voteCount * eachVotePay)`) at [6](#0-5)  and in `IncentiveManager.reward` at [7](#0-6) , but those two are triggered internally during block maintenance (not by a user transaction), whereas `computeReward(cycle, votes)` is directly reachable by a plain user transaction/query.

### Impact Explanation
Every voter whose reward is computed via the legacy cycle-by-cycle path accumulates a rounding/truncation error on each cycle's reward. Over many cycles and many voters this systematically miscalculates (generally undercounts, occasionally overcounts depending on rounding direction) the amount of TRX allowance credited to accounts, i.e. an inaccurate/unbacked-balance style discrepancy in stake-reward accounting. This matches the "unbacked balance" / "theft or permanent freezing of funds" impact class for reward math, since users can receive systematically wrong (lost) reward amounts that never reconcile with the actual total distributed by the witness.

### Likelihood Explanation
The buggy code path is only exercised for cycles before `getNewRewardAlgorithmEffectiveCycle()` when `allowOldRewardOpt()` is disabled — i.e., it is a legacy branch, not the current default reward algorithm used since the Vi-based reward optimization was introduced. On networks/cycles where the new reward algorithm and `allowOldRewardOpt` are already active (as they are on current mainnet), this legacy branch is not exercised for new rewards, which significantly lowers real-world likelihood; it would only matter for computing/re-verifying rewards on historical cycles prior to those upgrades, or on a fresh/private network before the upgrade flags are enabled.

### Recommendation
Reorder the arithmetic to multiply before dividing, and prefer integer/BigInteger math over `double` for reward accounting to avoid floating-point precision loss, mirroring the fix already applied to the newer Vi-based reward computation (`deltaVi.multiply(BigInteger.valueOf(userVote)).divide(DECIMAL_OF_VI_REWARD)` in the same file). Concretely, change:
```
double voteRate = (double) userVote / totalVote;
reward += voteRate * totalReward;
```
to a multiply-first, BigInteger-based computation such as:
```
reward += BigInteger.valueOf(userVote).multiply(BigInteger.valueOf(totalReward))
    .divide(BigInteger.valueOf(totalVote)).longValueExact();
```
The same fix should be applied to `payStandbyWitness()` and `IncentiveManager.reward()` for consistency, even though those are not directly reachable by a user transaction.

### Proof of Concept
1. On a chain/cycle where `allowOldRewardOpt()` is false and `beginCycle < newAlgorithmCycle` (legacy reward path active), have account A vote for witness W with `userVote = 7` out of `totalVote = 3` (i.e., other voters combined hold votes such that ratios produce non-terminating decimals), and let `totalReward = 1_000_000_007` for that cycle.
2. Call `WithdrawBalanceActuator` for a related delegate/query call that triggers `MortgageService.withdrawReward`/`queryReward`, which internally calls `computeReward(cycle, votes)`.
3. Compare the value produced by `voteRate = (double)7/3; reward = (long)(voteRate * 1_000_000_007)` against the mathematically correct integer result `7 * 1_000_000_007 / 3`. The double-based computation truncates/rounds differently than the exact integer division, demonstrating the reward discrepancy caused by dividing before multiplying.
(Exact reproduction requires setting up `DelegationStore` reward/vote fixtures and driving `WithdrawBalanceActuator`/`queryReward` end-to-end, which was not executed here — the analysis is based on direct code inspection of the arithmetic in `MortgageService.computeReward` and its call graph.)

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

**File:** chainbase/src/main/java/org/tron/core/service/MortgageService.java (L108-126)
```java
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
```

**File:** chainbase/src/main/java/org/tron/core/service/MortgageService.java (L153-167)
```java
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

**File:** consensus/src/main/java/org/tron/consensus/dpos/IncentiveManager.java (L34-42)
```java
    long totalPay = consensusDelegate.getWitnessStandbyAllowance();
    for (ByteString witness : witnesses) {
      byte[] address = witness.toByteArray();
      long pay = (long) (consensusDelegate.getWitness(address).getVoteCount() * ((double) totalPay
          / voteSum));
      AccountCapsule accountCapsule = consensusDelegate.getAccount(address);
      accountCapsule.setAllowance(accountCapsule.getAllowance() + pay);
      consensusDelegate.saveAccount(accountCapsule);
    }
```
