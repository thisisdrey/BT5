### Title
Witness rewards permanently lost when `witnessVi` accumulator is not updated for zero-vote witnesses - (File: `chainbase/src/main/java/org/tron/core/store/DelegationStore.java`)

### Summary
The reported Astaria bug is that `yIntercept` (the accumulator that tracks accrued-but-unrealized value) is not updated when `slope` (the rate term) goes to zero, so already-earned fees are silently dropped from the accounting model and permanently lost to liquidity providers. Java-tron's DPoS voting-reward system uses an analogous linear accumulator model — the per-witness `Vi` value (`witnessVi`) — to track cumulative reward-per-vote so that voters can later compute their pro-rata share via `deltaVi * userVote`. When a witness earns block/standby/transaction-fee rewards while its `voteCount` is `0`, the `Vi` accumulator is never incremented to reflect that reward, exactly like `yIntercept` never being bumped when the lien slope hits zero — the reward is recorded in the reward ledger but becomes permanently unrecoverable by any voter.

### Finding Description
`MortgageService.payReward()` unconditionally records a witness's non-brokerage reward share into the `DelegationStore` via `addReward(cycle, witnessAddress, value)`, independent of whether that witness currently has any votes: [1](#0-0) 

This is invoked unconditionally for every block from `Manager.payReward(block)` for the block producer, and from `payStandbyWitness()` for the top-127 standby set: [2](#0-1) 

Every maintenance cycle, `MaintenanceManager.doMaintenance()` converts that per-cycle reward ledger into the `Vi` (reward-per-vote) accumulator used for later payout computation, passing in the witness's *current* `voteCount`: [3](#0-2) 

The accumulator update itself is the root of the bug — when `voteCount == 0` (regardless of whether `reward > 0`), the function takes the "just forward pre `Vi`" branch and never folds the recorded reward into the accumulator: [4](#0-3) 

The same faulty logic is duplicated in `RewardViCalService.accumulateWitnessVi()`: [5](#0-4) 

All later reward computation (`MortgageService.computeReward()` / `VoteRewardUtil.computeReward()`) is purely a function of `deltaVi = endVi - beginVi` multiplied by the caller's vote count: [6](#0-5) 

Because `deltaVi` for that cycle is `0` (the reward never entered `Vi`), no voter — now or ever — can claim that cycle's reward for that witness. The value stored via `addReward()` is orphaned: there is no code path that returns it to the witness, to voters, or to the black hole; it is simply unreachable forever once the cycle passes, which is functionally a permanent loss/freezing of newly-minted TRX intended as staking reward.

This state is reachable without any malicious actor: any witness that is part of the active (top-N) or standby (top-127) set with `voteCount == 0` — e.g., a freshly `WitnessCreateContract`-registered witness on a chain/testnet where the number of witnesses is below the active/standby threshold, or immediately after registration before it receives its first vote — will accumulate block/standby rewards that get silently discarded during the next `doMaintenance()`.

### Impact Explanation
This causes permanent, unrecoverable loss of protocol-issued reward funds: TRX intended to be distributed pro-rata to voters (or reclaimable via governance) vanishes from the accounting model with no path to recovery, mirroring the "LPs never earn fees / lose part of provided liquidity" impact in the original report. Because it silently corrupts the reward-accounting invariant (total rewards recorded in `DelegationStore` should equal total ever paid out), it also creates a permanent discrepancy between minted supply and claimable supply.

### Likelihood Explanation
The trigger condition — an active or standby witness with zero votes earning block/standby/tx-fee rewards — is a normal, non-malicious network state (new witness registration, low-participation networks, private/test deployments, or early mainnet-like bootstrap phases before voting activity). No privileged action or attacker cooperation is required; it happens automatically through ordinary block production (`Manager.processBlock` → `payReward`) and the periodic maintenance cycle (`MaintenanceManager.doMaintenance`).

### Recommendation
In `DelegationStore.accumulateWitnessVi` (and the duplicated logic in `RewardViCalService.accumulateWitnessVi`), do not silently drop the reward when `voteCount == 0`. Either:
- Redirect/refund the reward (e.g., credit it to `allowance` directly, or route it to the black hole / next cycle's pool) when there are no votes to distribute to, or
- Prevent `addReward` from being recorded for a witness whose `voteCount` is 0 at reward time, ensuring the minted amount is not orphaned in the reward ledger.

### Proof of Concept
1. Register a new witness via `WitnessCreateContract` (unprivileged tx) on a network where the witness count is below the active/standby threshold, so it is immediately part of the active/standby set with `voteCount == 0`.
2. Let blocks be produced with this witness as the block producer: `Manager.payReward(block)` → `MortgageService.payBlockReward()` → `payReward()` → `delegationStore.addReward(cycle, witnessAddress, value)` accumulates a nonzero reward for `cycle` even though `voteCount == 0`.
3. At the next maintenance boundary, `MaintenanceManager.doMaintenance()` calls `delegationStore.accumulateWitnessVi(curCycle, witness, 0)`, which takes the "forward pre `Vi`" branch — the recorded reward is never folded into `witnessVi`.
4. Any voter who votes for this witness afterward (or the witness itself, via `withdrawReward`/`WithdrawRewardProcessor`) computes `deltaVi` for that cycle as `0`, so the reward recorded in step 2 is never paid to anyone — permanently lost.

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

**File:** chainbase/src/main/java/org/tron/core/service/MortgageService.java (L215-228)
```java
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
```

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L1946-1965)
```java
  private void payReward(BlockCapsule block) {
    WitnessCapsule witnessCapsule =
        chainBaseManager.getWitnessStore().getUnchecked(block.getInstance().getBlockHeader()
            .getRawData().getWitnessAddress().toByteArray());
    if (getDynamicPropertiesStore().allowChangeDelegation()) {
      mortgageService.payBlockReward(witnessCapsule.getAddress().toByteArray(),
          getDynamicPropertiesStore().getWitnessPayPerBlock());
      mortgageService.payStandbyWitness();

      if (chainBaseManager.getDynamicPropertiesStore().supportTransactionFeePool()) {
        long transactionFeeReward = floorDiv(
            chainBaseManager.getDynamicPropertiesStore().getTransactionFeePool(),
                Constant.TRANSACTION_FEE_POOL_PERIOD,
            chainBaseManager.getDynamicPropertiesStore().disableJavaLangMath());
        mortgageService.payTransactionFeeReward(witnessCapsule.getAddress().toByteArray(),
            transactionFeeReward);
        chainBaseManager.getDynamicPropertiesStore().saveTransactionFeePool(
            chainBaseManager.getDynamicPropertiesStore().getTransactionFeePool()
                - transactionFeeReward);
      }
```

**File:** consensus/src/main/java/org/tron/consensus/dpos/MaintenanceManager.java (L96-101)
```java
    if (dynamicPropertiesStore.useNewRewardAlgorithm()) {
      long curCycle = dynamicPropertiesStore.getCurrentCycleNumber();
      consensusDelegate.getAllWitnesses().forEach(witness -> {
        delegationStore.accumulateWitnessVi(curCycle, witness.createDbKey(), witness.getVoteCount());
      });
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

**File:** chainbase/src/main/java/org/tron/core/service/RewardViCalService.java (L209-229)
```java
  private void accumulateWitnessReward(byte[] witness) {
    long startCycle = 1;
    LongStream.range(startCycle, newRewardCalStartCycle)
        .forEach(cycle -> accumulateWitnessVi(cycle, witness));
  }

  private void accumulateWitnessVi(long cycle, byte[] address) {
    BigInteger preVi = getWitnessVi(cycle - 1, address);
    long voteCount = getWitnessVote(cycle, address);
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
