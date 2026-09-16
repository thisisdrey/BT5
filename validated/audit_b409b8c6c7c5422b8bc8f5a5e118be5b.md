### Title
Division-before-multiplication precision loss in old-algorithm SR vote reward calculation - (File: `chainbase/src/main/java/org/tron/core/service/MortgageService.java`)

### Summary
`MortgageService.computeReward(long cycle, List<Pair<byte[], Long>> votes)` computes a voter's share of a witness's total reward by first dividing `userVote / totalVote` into a `double`, then multiplying by `totalReward`. This is the same "divide before multiply" pattern flagged in the referenced Sherlock report (`AaveLeverageModule._updateCollateralPosition`), and it is reachable from an ordinary, unprivileged account action: withdrawing or querying staking rewards.

### Finding Description
In `MortgageService.java`:
```java
private long computeReward(long cycle, List<Pair<byte[], Long>> votes) {
  ...
  long totalVote = delegationStore.getWitnessVote(cycle, srAddress);
  ...
  long userVote = vote.getValue();
  double voteRate = (double) userVote / totalVote;
  reward += voteRate * totalReward;
}
``` [1](#0-0) 

This is called by `computeReward(long beginCycle, long endCycle, AccountCapsule accountCapsule)` for every cycle before `newRewardAlgorithmEffectiveCycle` (the "old algorithm" path), which in turn is invoked from `withdrawReward(byte[] address)` and `queryReward(byte[] address)`: [2](#0-1) [3](#0-2) 

`withdrawReward` is reachable from a normal signed transaction via `WithdrawBalanceActuator` and `VoteWitnessActuator.countVoteAccount`, and `queryReward` is reachable via the `GetRewardServlet` / `Wallet.getReward` API and `VoteRewardUtil` for TVM `vote` precompile calls. Any unprivileged account holder that has voted for an SR before the new-reward-algorithm cutover can trigger this arithmetic by simply withdrawing or querying its reward.

The precision loss arises because `(double) userVote / totalVote` truncates to double precision *before* being multiplied by `totalReward`; the mathematically correct order is `userVote * totalReward / totalVote` (multiply first, then divide), which the report's referenced formula shows minimizes rounding error. Doing division first discards low-order bits of the ratio, and the subsequent multiplication amplifies that already-rounded value, producing results that can differ from the precise value — exactly analogous to the Aave `preciseDiv`/`divDown` ordering bug in the report.

A structurally identical pattern also exists in `payStandbyWitness()`:
```java
double eachVotePay = (double) totalPay / voteSum;
long pay = (long) (w.getVoteCount() * eachVotePay);
``` [4](#0-3) 
This runs on every block via `Manager.payReward` → `mortgageService.payStandbyWitness()` [5](#0-4) , so it is triggered automatically as part of normal block production rather than by a single attacker-controlled transaction, but it shares the same root cause.

By contrast, the codebase's "new reward algorithm" path (`computeReward` using `WitnessVi`/`DECIMAL_OF_VI_REWARD`, `RewardViCalService`, `VoteRewardUtil.computeReward`) correctly multiplies before dividing using `BigInteger` arithmetic, and the resource/energy calculations in `EnergyProcessor`/`RepositoryImpl` were explicitly "hardened" (see `hardenCalculation()`/`hardenResourceCalculation()` and associated tests) to fix exactly this precision-loss class for bandwidth/energy math. The old-algorithm SR reward path in `MortgageService.computeReward(long cycle, ...)` was apparently never hardened in the same way.

### Impact Explanation
The impact is a systematic (not one-off) rounding/precision loss in reward accounting for votes cast under the pre-cutover reward algorithm. This can cause voters to receive slightly less (or, in edge cases, more) TRX allowance than the mathematically correct pro-rata share of a witness's reward pool, i.e., a form of unbacked/miscalculated balance in the protocol's internal reward accounting. Because `double` arithmetic is used for real TRX-denominated amounts (`sun`), and division happens before multiplication, the error is not merely a 1-sun rounding artifact but a genuine loss of significant digits when `userVote`/`totalVote` have large magnitude differences — mirroring the report's own numeric example precision gap. Given this governs reward distribution to real (though historical/legacy-cycle) voters, this is a Medium-severity accounting/precision defect rather than a High-impact fund-draining bug, since it does not grant unauthorized withdrawal or allow an attacker to steal others' funds directly — it silently misallocates reward dust across all voters using the old algorithm.

### Likelihood Explanation
Likelihood is high for triggering the code path (any voter with pre-cutover cycles calling `withdrawReward`/`queryReward`, or the automatic per-block `payStandbyWitness` call), but the practical magnitude of any single instance's precision loss is small (sub-unit rounding). It requires no special privileges — an ordinary account holder or a normal block-reward cycle exercises this code continuously.

### Recommendation
In `MortgageService.computeReward(long cycle, List<Pair<byte[], Long>> votes)`, replace the double-based `voteRate * totalReward` computation with multiply-then-divide integer/BigInteger arithmetic, e.g. `BigInteger.valueOf(userVote).multiply(BigInteger.valueOf(totalReward)).divide(BigInteger.valueOf(totalVote))`, consistent with the pattern already used for the new reward algorithm (`WitnessVi`/`DECIMAL_OF_VI_REWARD`) and with the "hardened" resource calculations in `EnergyProcessor`/`RepositoryImpl`. Apply the same fix to `payStandbyWitness()`'s `eachVotePay` computation.

### Proof of Concept
Given (analogous to the report's own numeric example): `userVote = 10_000_000_000_030_000`-scale values divided into `totalVote` before multiplying by `totalReward` produces a truncated `double` ratio whose subsequent multiplication yields a smaller (or different) result than first computing `userVote * totalReward` (exact in `BigInteger`) and dividing by `totalVote` once. This can be reproduced directly against `MortgageService.computeReward(long, List<Pair<byte[],Long>>)` [1](#0-0)  by choosing `userVote`, `totalVote`, `totalReward` values (e.g., large `totalReward` with a `totalVote` that is not an exact divisor of `userVote * totalReward`) and comparing the double-based result to the exact `BigInteger` multiply-then-divide result, as already done for the harden-calculation regression tests in `CalculateGlobalLimitHardenTest`/`RepositoryImplHardenTest` for the analogous energy/bandwidth computations.

### Citations

**File:** chainbase/src/main/java/org/tron/core/service/MortgageService.java (L53-66)
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
```

**File:** chainbase/src/main/java/org/tron/core/service/MortgageService.java (L89-169)
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

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L1950-1953)
```java
    if (getDynamicPropertiesStore().allowChangeDelegation()) {
      mortgageService.payBlockReward(witnessCapsule.getAddress().toByteArray(),
          getDynamicPropertiesStore().getWitnessPayPerBlock());
      mortgageService.payStandbyWitness();
```
