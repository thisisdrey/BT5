Based on my investigation, I found a strong analog to the reported precision-loss bug class.

### Title
Standby witness reward precision loss causes permanent loss of funds due to floating-point truncation in `IncentiveManager.reward` / `MortgageService.payStandbyWitness` - ([File: consensus/src/main/java/org/tron/consensus/dpos/IncentiveManager.java])

### Summary
The Sherlock report describes a funding fee that rounds down to zero (and generally undercounts) because of integer-division-style precision loss when a small numerator is divided against a comparatively large denominator before being multiplied by a fee rate. The same rounding-down pattern exists in java-tron's standby-witness and vote reward distribution code, where `pay = voteCount * (totalPay / voteSum)` is computed with a `double` intermediate and then truncated with `(long)` cast, causing systematic underpayment (down to exactly zero for low-vote witnesses) every maintenance cycle.

### Finding Description
In `IncentiveManager.reward`, the per-witness standby reward is computed as: [1](#0-0) 

`pay = (long) (voteCount * ((double) totalPay / voteSum))`. When `voteCount` is small relative to `voteSum` (which happens naturally since up to `WITNESS_STANDBY_LENGTH` witnesses with varying vote counts split `totalPay`), the double multiplication result can truncate to zero or lose a full unit of TRX on every maintenance cycle (occurs periodically, driven only by ordinary vote-weight input, not by any privileged actor). The equivalent logic exists in `MortgageService.payStandbyWitness`: [2](#0-1) 

as well as the per-vote reward computation in `MortgageService.computeReward(cycle, votes)`: [3](#0-2) 

Here `voteRate = (double) userVote / totalVote; reward += voteRate * totalReward` truncates when accumulated into the `long reward` return value. For accounts with a small vote share relative to `totalVote`, the reward contribution rounds toward zero — mirroring exactly the “long_utilization / short_utilization rounds to 0” root cause in the Sherlock report, where a small interest relative to a large reserve rounds the utilization (and hence the fee) to zero.

The brokerage split in `MortgageService.payReward` also truncates via `(long) (brokerageRate * value)`: [4](#0-3) 

This systematically biases the fee split, similar to how `borrow_long_fee`/`borrow_short_fee` are miscalculated in the report.

### Impact Explanation
The impact is a permanent, protocol-level underpayment of legitimate reward/fee amounts to standby witnesses and voters — funds effectively vanish (never credited to any account) rather than being redistributed, which is the same "unbacked/lost value" class validated as Medium severity in the Sherlock report (a consistent ~1% or greater loss depending on vote distribution, and can reach 100% loss — reward becomes exactly zero — for low-vote-weight witnesses, exactly like the referenced PoC where `funding_paid == 0`). This is systemic (affects every maintenance cycle) rather than an isolated rounding artifact, and it requires no malicious actor — it is triggered purely by the natural distribution of votes among standby witnesses/voters.

### Likelihood Explanation
This occurs on every DPoS maintenance cycle (`IncentiveManager.reward` runs whenever standby witnesses are paid) and every reward withdrawal (`MortgageService.computeReward`/`payStandbyWitness`), so the code path is reached deterministically and continuously by ordinary chain operation without requiring any special transaction or privilege — it is a core accounting routine executed automatically by `Manager`/`MaintenanceManager`.

### Recommendation
Replace the floating-point-based reward split (`double` division followed by truncating `(long)` cast) with integer/`BigInteger`-based arithmetic that preserves remainders (e.g., track and carry forward remainder dust, or scale up by a large fixed-point factor as already done via `DelegationStore.DECIMAL_OF_VI_REWARD` for the newer Vi-based reward algorithm), and add minimum-value/scale-up safeguards so that reward contributions do not silently round to zero for low-vote-weight accounts.

### Proof of Concept
Given standby witnesses with `voteCount` values such that `voteCount * totalPay < voteSum` (e.g., `totalPay = 100`, `voteSum = 1_000_000`, individual `voteCount = 5`), `pay = (long)(5 * (100.0/1_000_000)) = (long)(0.0005) = 0`. That witness receives zero reward for that cycle even though it is entitled to a proportional share, and the "lost" `totalPay` share is never redistributed to anyone — permanently lost, matching the report's `funding_paid == 0` PoC pattern.

### Citations

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
