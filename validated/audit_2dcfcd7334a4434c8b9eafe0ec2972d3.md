### Title
Zero-Vote Active Witness Reward Permanently Lost/Stuck Due to Skipped VI Accumulation - (File: chainbase/src/main/java/org/tron/core/service/RewardViCalService.java, chainbase/src/main/java/org/tron/core/store/DelegationStore.java, consensus/src/main/java/org/tron/consensus/dpos/MaintenanceManager.java)

### Summary
The reported Thena bug ("killing a gauge" mid-period leaves its already-accrued vote weight inside `totalWeightsPerEpoch`, so a share of emissions is permanently uncollectable) has a structural analog in java-tron's witness reward accounting. When a witness is part of the active Super Representative (SR) set but has a vote count of `0` for a closing cycle, the block/transaction-fee reward already credited to that witness in `DelegationStore` for that cycle is silently dropped by `accumulateWitnessVi`/`computeReward` because the accumulation step is skipped whenever `voteCount == 0`. The corresponding TRX, however, has already been deducted from the live `TransactionFeePool`, so it becomes permanently unaccounted for — effectively burned/stuck, exactly like the killed gauge's reward share in the original report.

### Finding Description
Every block, `Manager.payReward` pays the block producer unconditionally regardless of its current vote count: [1](#0-0) 

`MortgageService.payReward` records this pay into `DelegationStore` keyed by the current cycle and witness address, independent of whether the witness has any votes: [2](#0-1) 

For chains using the new VI-based reward algorithm, at each maintenance the recorded per-cycle reward is only turned into a claimable delta (`deltaVi`) if `voteCount != 0`. If `voteCount == 0`, the previous VI is simply forwarded and the reward for that cycle is discarded, never distributed to anyone: [3](#0-2) 

This accumulation is driven every maintenance cycle for all current active witnesses using their standing `voteCount`: [4](#0-3) 

A witness can legitimately have `voteCount == 0` while still being part of the active SR set: `DposService.updateWitness` only truncates to the top `MAX_ACTIVE_WITNESS_NUM` (27) witnesses by vote count — if the total number of registered witnesses is at or below this threshold (a normal condition on new, private, or low-participation TRON-based chains, and also possible transiently on any chain right after a new witness self-registers before receiving votes), *all* registered witnesses, including ones with zero votes, become active and are scheduled to produce blocks: [5](#0-4) 

Meanwhile, transaction-fee rewards paid to such a witness are physically deducted from the live `TransactionFeePool` balance at the moment of payment: [6](#0-5) 

Because the reward entry recorded under that witness/cycle in `DelegationStore` is never turned into a VI delta (voteCount==0 branch), no account's `allowance` is ever credited for it — the withdrawn pool balance has no destination and is permanently unaccounted for. This mirrors the original report precisely: an accounting bucket (`totalWeightsPerEpoch` there, the per-witness/cycle reward record here) retains a portion of the total reward that can never be attributed to any beneficiary once the underlying weight is zero, while the corresponding funds have already left the live pool.

### Impact Explanation
TRX already removed from `TransactionFeePool` (and, in the standard reward-pay path, the notionally-allocated block reward) becomes permanently stuck/burned — it cannot be claimed by the witness (it has no voters to attribute VI delta to) nor by anyone else, and it is not returned to the pool. This is a permanent loss/freezing-of-funds bug in the core consensus reward accounting, reachable without any privileged action once a low/zero-vote witness is part of the active SR rotation.

### Likelihood Explanation
No malicious SR/witness/committee/network behavior is required — an honest witness with zero votes (e.g. newly self-registered via the unprivileged `WitnessCreateContract`, on a chain with ≤27 total registered witnesses, or transiently right after registration before it accrues votes) will unavoidably trigger this path simply by taking its normal turn producing blocks in the DPoS schedule. The condition (total witnesses ≤ `MAX_ACTIVE_WITNESS_NUM`) is common on new/private/consortium java-tron deployments and can also occur transiently on any deployment.

### Recommendation
When accumulating witness VI at maintenance (`DelegationStore.accumulateWitnessVi` / `RewardViCalService.accumulateWitnessVi`), do not silently drop a nonzero `reward` when `voteCount == 0`: either roll the un-distributable reward back into the `TransactionFeePool`/general reward pool, or accrue it for later distribution once the witness attracts votes, so funds already withdrawn from a live balance are never left with no valid recipient.

### Proof of Concept
1. On a java-tron network where the number of registered witnesses is ≤ `MAX_ACTIVE_WITNESS_NUM` (27) (true for most private/consortium chains and achievable transiently on any chain), broadcast a `WitnessCreateContract` from any unprivileged account to register a new witness with `voteCount = 0`.
2. Because `DposService.updateWitness` includes all registered witnesses when the total is ≤ 27, the new witness is immediately part of the active SR set and gets scheduled to produce blocks.
3. When the new witness produces a block, `Manager.payReward` → `MortgageService.payBlockReward`/`payTransactionFeeReward` credits `DelegationStore.addReward(cycle, witnessAddress, value)` and, for the fee-pool portion, actually decrements `TransactionFeePool`.
4. At the next maintenance cycle, `MaintenanceManager.doMaintenance` calls `delegationStore.accumulateWitnessVi(curCycle, witness, witness.getVoteCount())` with `voteCount = 0`; per `DelegationStore.accumulateWitnessVi`, the reward is dropped (only `preVi` is forwarded).
5. The TRX already removed from `TransactionFeePool` in step 3 is never credited to any account's `allowance`, permanently freezing those funds.

### Citations

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L1950-1965)
```java
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

**File:** consensus/src/main/java/org/tron/consensus/dpos/MaintenanceManager.java (L96-101)
```java
    if (dynamicPropertiesStore.useNewRewardAlgorithm()) {
      long curCycle = dynamicPropertiesStore.getCurrentCycleNumber();
      consensusDelegate.getAllWitnesses().forEach(witness -> {
        delegationStore.accumulateWitnessVi(curCycle, witness.createDbKey(), witness.getVoteCount());
      });
    }
```

**File:** consensus/src/main/java/org/tron/consensus/dpos/DposService.java (L178-186)
```java
  public void updateWitness(List<ByteString> list) {
    consensusDelegate.sortWitness(list);
    if (list.size() > MAX_ACTIVE_WITNESS_NUM) {
      consensusDelegate
          .saveActiveWitnesses(list.subList(0, MAX_ACTIVE_WITNESS_NUM));
    } else {
      consensusDelegate.saveActiveWitnesses(list);
    }
  }
```
