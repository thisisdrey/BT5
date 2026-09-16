Confirmed: `WithdrawBalanceActuator.execute` (`actuator/src/main/java/org/tron/core/actuator/WithdrawBalanceActuator.java:54`) calls `MortgageService.withdrawReward`, which for pre-new-algorithm cycles routes through `computeReward(long cycle, List<Pair<byte[], Long>> votes)` — the exact rounding-dust pattern analogous to the reported bug.

### Title
Per-voter reward truncation in old reward algorithm permanently strands SR reward-pool dust - (File: `chainbase/src/main/java/org/tron/core/service/MortgageService.java`)

### Summary
`MortgageService.computeReward(long cycle, List<Pair<byte[], Long>> votes)` splits a fixed, already-allocated per-SR-per-cycle reward pool among that SR's voters by computing each voter's share independently with floating point, then truncating to `long`. Because each share is rounded down independently instead of being derived as "pool minus previously distributed shares," the sum of all distributed shares is systematically less than the pool amount, and the shortfall is never credited to anyone — it is silently and permanently lost, exactly mirroring the `admin_amount` dust-accumulation pattern described in the external report.

### Finding Description
`payReward` allocates a specific TRX amount into the reward pool for a witness/cycle via `delegationStore.addReward(cycle, witnessAddress, value)` [1](#0-0) . When a voter withdraws or queries a reward for cycles before `newRewardAlgorithmEffectiveCycle`, `getOldReward` iterates cycle by cycle and calls `computeReward(cycle, votes)` for each voter [2](#0-1) .

Inside `computeReward(long cycle, List<Pair<byte[], Long>> votes)`, for each voter of a given SR/cycle, `totalReward` is the fixed pool amount stored by `payReward`, and each voter's share is computed as `voteRate * totalReward` using `double` arithmetic and then narrowed to `long` via the `+=` compound assignment (implicit truncating cast): [3](#0-2) 

Because `voteRate = userVote / totalVote` is computed and applied independently for every voter rather than as "remaining pool minus shares already paid to other voters," the sum `Σ(voteRate_i * totalReward)` truncated per-voter is, in general, strictly less than `totalReward`. The residual (`totalReward - Σ distributed shares`) is never added back to any account, any pool, or any recoverable store — `delegationStore.getReward(cycle, srAddress)` is not decremented to reflect what was actually paid out, and there is no sweep/administrator credit for the shortfall. This is the same root-cause class flagged in the external report for `calcCommissions`: independently rounding each split of a fixed total instead of deriving one component as `total - sum(other components)`.

### Impact Explanation
Every time a voter withdraws pre-new-algorithm cycle rewards (via `WithdrawBalanceActuator` → `MortgageService.withdrawReward` → `computeReward`), or simply queries them (`queryReward`), rounding dust from that SR's per-cycle reward pool is permanently unrecoverable by any account — the tokens were minted/allocated into the reward pool via `payReward` but can never be fully claimed. Aggregated across many SRs, many cycles, and many voters (particularly SRs with many small-stake voters, where per-voter truncation is more frequent and material relative to `totalReward`), this results in a persistent, unrecoverable loss of already-allocated reward funds — matching the "permanent freezing/loss of funds" bar, at Medium severity given the amounts are individually small but accumulate over the protocol's operating lifetime, consistent with the original report's own severity rating.

### Likelihood Explanation
This code path is reachable by any ordinary account holder who voted for an SR in an old-algorithm-era cycle, simply by broadcasting a standard, unprivileged `WithdrawBalanceContract` transaction — no special privileges are required. It is guaranteed to trigger whenever `beginCycle < newRewardAlgorithmEffectiveCycle` for the withdrawing account and the SR has more than one voter with non-trivial vote-count ratios, which is the common case for any actively-voted SR from the legacy reward era.

### Recommendation
Do not compute each voter's reward share independently via truncating floating-point multiplication. Instead, either (a) use exact integer/`BigInteger` arithmetic consistent with the newer VI-based algorithm (`computeReward(long beginCycle, long endCycle, AccountCapsule accountCapsule)` at lines 199-230, which already uses `BigInteger` deltaVi math), or (b) track cumulative amount already distributed per SR/cycle and compute the last voter's (or any voter's) share as `remainingPool - alreadyDistributed` so the sum of distributed shares always exactly equals `totalReward`, eliminating unclaimable dust.

### Proof of Concept
1. For a cycle `c` before `newRewardAlgorithmEffectiveCycle`, `payReward` allocates `delegationStore.addReward(c, srAddress, totalReward)` with, e.g., `totalReward = 100`.
2. Three voters vote for this SR with `userVote` = 1, 1, 1 and `totalVote` = 3.
3. `voteRate` for each voter = `1/3 = 0.333...`; each voter's computed share = `(long)(0.333... * 100)` = `33` (truncated).
4. Sum distributed to all three voters = `33 * 3 = 99`, while `totalReward = 100`.
5. The remaining `1` unit is never credited anywhere — it is not re-added to any voter, to the SR, or to any pool — and is permanently unclaimable, repeating every cycle and every SR with a similar voter distribution. [3](#0-2) [4](#0-3)

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

**File:** actuator/src/main/java/org/tron/core/actuator/WithdrawBalanceActuator.java (L54-55)
```java
    mortgageService.withdrawReward(withdrawBalanceContract.getOwnerAddress()
        .toByteArray());
```
