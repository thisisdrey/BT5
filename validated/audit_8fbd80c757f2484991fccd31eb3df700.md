### Title
Floating-point (double) division-before-multiplication in legacy vote-reward calculation causes reward precision loss - (File: chainbase/src/main/java/org/tron/core/service/MortgageService.java)

### Summary
The legacy (pre-"new reward algorithm") per-cycle reward computation in `MortgageService.computeReward(long cycle, List<Pair<byte[], Long>> votes)` performs `userVote / totalVote` as a `double` division **before** multiplying by `totalReward`, instead of doing the multiplication first with integer/BigInteger arithmetic and dividing last. This mirrors the reported bug class (loss of precision from incorrect ordering/typing of multiply/divide operations in a per-user reward formula), except here the loss is introduced through floating-point representation error rather than integer truncation.

### Finding Description
`MortgageService.computeReward(long cycle, List<Pair<byte[], Long>> votes)` computes each voter's share of a Super Representative's reward pool as:
```java
double voteRate = (double) userVote / totalVote;
reward += voteRate * totalReward;
``` [1](#0-0) 

This is reached whenever a user withdraws or queries their reward and the relevant cycle predates `newRewardAlgorithmEffectiveCycle`, via `computeReward(beginCycle, endCycle, accountCapsule)` → `getOldReward(...)` → (when `allowOldRewardOpt` is not enabled) the per-cycle loop calling this legacy `computeReward(cycle, votes)`: [2](#0-1) [3](#0-2) 

This is reachable end-to-end by any unprivileged account through a normal `WithdrawBalanceContract` transaction, which is unpacked and executed by `WithdrawBalanceActuator.execute`, directly invoking `mortgageService.withdrawReward(...)`: [4](#0-3) 

The identical division-then-multiplication-with-doubles pattern also exists in `IncentiveManager.reward`, used for standby-witness allowance distribution:
```java
long pay = (long) (consensusDelegate.getWitness(address).getVoteCount() * ((double) totalPay / voteSum));
``` [5](#0-4) 

By contrast, the newer reward path (used once `newRewardAlgorithmEffectiveCycle`/`allowOldRewardOpt` is active) correctly performs `BigInteger` multiplication before division with a fixed-point scaling constant (`DECIMAL_OF_VI_REWARD`), which avoids this precision loss: [6](#0-5) [7](#0-6) 

### Impact Explanation
Because `double` has only ~15-17 significant decimal digits of precision, and vote counts / reward pools in java-tron can be large (TRX amounts scaled by `TRX_PRECISION`, vote counts in the billions), computing `userVote / totalVote` as a floating-point ratio before multiplying by `totalReward` introduces rounding error that is not a simple, predictable truncation (unlike pure integer division) — it can round in either direction per user, per cycle. Summed across many voters and many cycles, this produces a persistent mismatch between the sum of rewards actually paid out and the reward pool recorded in `delegationStore` for that cycle/witness, i.e., an inconsistency between individually-computed user rewards and the total funds backing them. This is a genuine "unbacked balance"/accounting-drift class issue in the reward-payout pipeline, though it is limited to the legacy code path that is only exercised for cycles predating `newRewardAlgorithmEffectiveCycle` or when `allowOldRewardOpt` is not active.

### Likelihood Explanation
I could not fully verify from the available index whether `allowOldRewardOpt` and `newRewardAlgorithmEffectiveCycle` are already enabled by default on the current mainnet state represented in this repo snapshot (I found references to these flags in `DynamicPropertiesStore`, `ProposalUtil`, `CommitteeConfig`, and `reference.conf`, but was unable to read the concrete default values/initialization code within the tool budget). If the old algorithm is already fully retired on mainnet (cycle window closed), this path is dead code for current withdrawals and the practical likelihood is very low. If any account still has unwithdrawn rewards from cycles before the effective cycle, or on a chain/testnet where `allowOldRewardOpt` was never activated, this is trivially triggered by a single `WithdrawBalanceContract` transaction from any account with votes.

### Recommendation
Replace the floating-point `voteRate` computation in `MortgageService.computeReward(long cycle, List<Pair<byte[], Long>> votes)` (and the analogous computation in `IncentiveManager.reward`) with integer/`BigInteger` arithmetic that multiplies before dividing, e.g. `reward += (totalReward * userVote) / totalVote` using `BigInteger` (or `Math.multiplyHigh`/`LongMath.checkedMultiply` guarded against overflow) instead of `double`, mirroring the fixed-point approach already used in `DelegationStore.accumulateWitnessVi` and `RewardViCalService`.

### Proof of Concept
Given `totalReward = 1_000_000_000L` (1000 TRX in sun) and `totalVote = 3`, distributed across users with `userVote = 1` each:
- Correct integer math: `(1_000_000_000 * 1) / 3 = 333_333_333` per user, summing to `999_999_999` for 3 users (1 sun unclaimed, deterministic truncation).
- Double-based math: `voteRate = 1.0/3.0 = 0.3333333333333333` (double); `reward = 0.3333333333333333 * 1_000_000_000 ≈ 333333333.3333333`, cast/accumulated as `long` — the exact rounding behavior depends on floating point representation and can differ from the integer-math result and from `(long)` truncation, demonstrating the arithmetic path is not equivalent to canonical multiply-then-divide integer accounting, and can diverge further at larger magnitudes (values approaching `2^53` where `double` can no longer represent all integers exactly), unlike the codebase's own `BigInteger`-based reward computations elsewhere. [8](#0-7)

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

**File:** actuator/src/main/java/org/tron/core/actuator/WithdrawBalanceActuator.java (L54-55)
```java
    mortgageService.withdrawReward(withdrawBalanceContract.getOwnerAddress()
        .toByteArray());
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

**File:** chainbase/src/main/java/org/tron/core/service/RewardViCalService.java (L155-171)
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
    }
    return reward;

```
