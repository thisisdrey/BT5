### Title
Global net/energy resource weight can be driven negative via TVM freeze/unfreeze precompiles, permanently distorting bandwidth/energy allocation for all accounts - (File: `actuator/src/main/java/org/tron/core/vm/repository/RepositoryImpl.java`)

### Summary
The `RepositoryImpl` implementation of `addTotalNetWeight`/`addTotalEnergyWeight`/`addTotalTronPowerWeight`, used exclusively by TVM native-contract processors (`FreezeBalanceV2Processor`, `UnfreezeBalanceV2Processor`, `CancelAllUnfreezeV2Processor`) reachable from a Solidity contract calling `freezeBalanceV2`/`unfreezeBalanceV2`/`cancelAllUnfreezeV2` opcodes, performs an unguarded `totalNetWeight += amount` and persists it, with **no floor at zero**, unlike the equivalent method on `DynamicPropertiesStore` used by the plain (non-TVM) actuators, which clamps the result with `max(0, totalNetWeight, ...)` when `allowNewReward()` is active.

### Finding Description
Two independent code paths update the chain-wide `TOTAL_NET_WEIGHT` / `TOTAL_ENERGY_WEIGHT` / `TOTAL_TRON_POWER_WEIGHT` dynamic properties, which are the global denominators used to compute every account's bandwidth/energy limit:

1. Direct actuators (`FreezeBalanceV2Actuator`, `UnfreezeBalanceV2Actuator`, `CancelAllUnfreezeV2Actuator`) call `dynamicStore.addTotalNetWeight(...)`, implemented in `chainbase/src/main/java/org/tron/core/store/DynamicPropertiesStore.java:2269-2306`, which explicitly clamps the accumulated weight to be non-negative: [1](#0-0) 

2. TVM native-contract processors invoked through the VM opcodes for `freezeBalanceV2`/`unfreezeBalanceV2`/`cancelAllUnfreezeV2` (i.e. reachable by any contract deployer/caller executing these precompiles from Solidity, see `Program.unfreezeBalanceV2`) instead call `repo.addTotalNetWeight(...)`, backed by `RepositoryImpl`, which has **no such floor**: [2](#0-1) 

These native-contract processors (e.g. `FreezeBalanceV2Processor.execute`, `UnfreezeBalanceV2Processor.updateTotalResourceWeight`, `CancelAllUnfreezeV2Processor.updateFrozenInfoAndTotalResourceWeight`) compute weight deltas as `(newWeight - oldWeight)` and pass them straight to `repo.addTotalNetWeight`/`addTotalEnergyWeight`/`addTotalTronPowerWeight`: [3](#0-2) [4](#0-3) 

Because state written by `RepositoryImpl` (via `updateDynamicProperty`) and state written by `DynamicPropertiesStore` share the same underlying keys (`TOTAL_NET_WEIGHT`, `TOTAL_ENERGY_WEIGHT`, `TOTAL_TRON_POWER_WEIGHT`), a sequence of TVM-triggered freeze/unfreeze operations that decreases the global weight without the compensating clamp can push `totalNetWeight`/`totalEnergyWeight` below zero (or leave it inconsistent versus the clamped value the non-TVM path would have produced). This mirrors the DYAD bug class: a state-dependent arithmetic path that is only "safe" under an implicit invariant (weight ≥ 0) which is enforced in one code path but not in a parallel, equally reachable code path.

`BandwidthProcessor.calculateGlobalNetLimit`/`calculateGlobalNetLimitV2` read `totalNetWeight` directly to compute every account's available bandwidth as `netWeight * (totalNetLimit / totalNetWeight)`: [5](#0-4) 
If `totalNetWeight` becomes negative or zero-but-should-be-positive due to the unguarded TVM path, this division either produces nonsensical (negative or wildly inflated) bandwidth/energy limits for every account network-wide, or (when `totalNetWeight == 0`) causes `calculateGlobalNetLimitV2`/`calculateGlobalNetLimit` to return `0`, meaning **every account's frozen-based bandwidth/energy limit collapses to zero** until the weight is organically restored by fresh freezes outside the TVM path — i.e., legitimate users' bandwidth/energy allocations get "stuck" at zero, analogous to Kerosene getting stuck until TVL exceeds supply in the original report.

### Impact Explanation
`totalNetWeight`/`totalEnergyWeight` are global, shared state consumed by every account's resource-limit computation in `BandwidthProcessor` and the analogous `EnergyProcessor`. A corruption here (driving the weight negative or to an inconsistent value relative to the clamped invariant maintained elsewhere) is not confined to the caller — it silently degrades or zeroes out bandwidth/energy limits for **all accounts on the network** until enough compensating freezes restore a positive weight, which is a protocol-wide resource-allocation halt affecting funds/fee availability for unrelated users. This satisfies the "node crash or halt" / "permanent freezing of funds/resources" bar for a Medium/High-severity finding, since it can be triggered by any contract deployer invoking the freeze/unfreeze precompiles from a smart contract — no privileged role required.

### Likelihood Explanation
Reachability requires only deploying a contract that calls the TVM native freeze/unfreeze precompiles (`freezeBalanceV2`, `unfreezeBalanceV2`, `cancelAllUnfreezeV2`), which are exposed to any transaction sender per `Program.java`. Triggering an actual negative/zero weight requires driving enough delta through the unguarded `RepositoryImpl` path relative to the account's own frozen balance changes (e.g., repeatedly freezing/unfreezing via contract calls interleaved with delegation changes), which is plausible but requires careful sequencing to exploit at scale — hence Medium likelihood rather than trivial.

### Recommendation
Apply the same non-negative clamp used in `DynamicPropertiesStore.addTotalNetWeight`/`addTotalEnergyWeight`/`addTotalTronPowerWeight` (i.e. `max(0, totalWeight, ...)` under `allowNewReward()`) inside `RepositoryImpl.addTotalNetWeight`/`addTotalEnergyWeight`/`addTotalTronPowerWeight`, so both code paths enforce the identical invariant, or route the TVM native-contract processors through `DynamicPropertiesStore`'s guarded implementation instead of `RepositoryImpl`'s unguarded one.

### Proof of Concept
Not independently executed; based on static code comparison between `RepositoryImpl.addTotalNetWeight` (`actuator/src/main/java/org/tron/core/vm/repository/RepositoryImpl.java:1196-1217`, unguarded) and `DynamicPropertiesStore.addTotalNetWeight` (`chainbase/src/main/java/org/tron/core/store/DynamicPropertiesStore.java:2269-2306`, guarded with `max(0, ...)`). A concrete exploit sequence (e.g. exact TVM call ordering to force a negative total weight in practice) was not verified against a running node/test harness within this investigation; confirming the exact triggering sequence and its downstream effect on `BandwidthProcessor`/`EnergyProcessor` would require running the existing `FreezeV2Test`/`UnfreezeBalanceV2ActuatorTest` test harnesses with TVM-triggered freeze/unfreeze calls and asserting on `DynamicPropertiesStore.getTotalNetWeight()`/`getTotalEnergyWeight()`.

### Citations

**File:** chainbase/src/main/java/org/tron/core/store/DynamicPropertiesStore.java (L2269-2293)
```java
  //The unit is trx
  public void addTotalNetWeight(long amount) {
    if (amount == 0) {
      return;
    }
    long totalNetWeight = getTotalNetWeight();
    totalNetWeight += amount;
    if (allowNewReward()) {
      totalNetWeight = max(0, totalNetWeight, disableJavaLangMath());
    }
    saveTotalNetWeight(totalNetWeight);
  }

  //The unit is trx
  public void addTotalEnergyWeight(long amount) {
    if (amount == 0) {
      return;
    }
    long totalEnergyWeight = getTotalEnergyWeight();
    totalEnergyWeight += amount;
    if (allowNewReward()) {
      totalEnergyWeight = max(0, totalEnergyWeight, disableJavaLangMath());
    }
    saveTotalEnergyWeight(totalEnergyWeight);
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/repository/RepositoryImpl.java (L1196-1217)
```java
  //The unit is trx
  @Override
  public void addTotalNetWeight(long amount) {
    long totalNetWeight = getTotalNetWeight();
    totalNetWeight += amount;
    saveTotalNetWeight(totalNetWeight);
  }

  //The unit is trx
  @Override
  public void addTotalEnergyWeight(long amount) {
    long totalEnergyWeight = getTotalEnergyWeight();
    totalEnergyWeight += amount;
    saveTotalEnergyWeight(totalEnergyWeight);
  }

  @Override
  public void addTotalTronPowerWeight(long amount) {
    long totalTronPowerWeight = getTotalTronPowerWeight();
    totalTronPowerWeight += amount;
    saveTotalTronPowerWeight(totalTronPowerWeight);
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/FreezeBalanceV2Processor.java (L83-101)
```java
    switch (param.getResourceType()) {
      case BANDWIDTH:
        long oldNetWeight = accountCapsule.getFrozenV2BalanceWithDelegated(BANDWIDTH) / TRX_PRECISION;
        accountCapsule.addFrozenBalanceForBandwidthV2(frozenBalance);
        long newNetWeight = accountCapsule.getFrozenV2BalanceWithDelegated(BANDWIDTH) / TRX_PRECISION;
        repo.addTotalNetWeight(newNetWeight - oldNetWeight);
        break;
      case ENERGY:
        long oldEnergyWeight = accountCapsule.getFrozenV2BalanceWithDelegated(ENERGY) / TRX_PRECISION;
        accountCapsule.addFrozenBalanceForEnergyV2(frozenBalance);
        long newEnergyWeight = accountCapsule.getFrozenV2BalanceWithDelegated(ENERGY) / TRX_PRECISION;
        repo.addTotalEnergyWeight(newEnergyWeight - oldEnergyWeight);
        break;
      case TRON_POWER:
        long oldTPWeight = accountCapsule.getTronPowerFrozenV2Balance() / TRX_PRECISION;
        accountCapsule.addFrozenForTronPowerV2(frozenBalance);
        long newTPWeight = accountCapsule.getTronPowerFrozenV2Balance() / TRX_PRECISION;
        repo.addTotalTronPowerWeight(newTPWeight - oldTPWeight);
        break;
```

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/UnfreezeBalanceV2Processor.java (L183-209)
```java
  public void updateTotalResourceWeight(AccountCapsule accountCapsule,
                                        Common.ResourceCode freezeType,
                                        long unfreezeBalance,
                                        Repository repo) {
    switch (freezeType) {
      case BANDWIDTH:
        long oldNetWeight = accountCapsule.getFrozenV2BalanceWithDelegated(BANDWIDTH) / TRX_PRECISION;
        accountCapsule.addFrozenBalanceForBandwidthV2(-unfreezeBalance);
        long newNetWeight = accountCapsule.getFrozenV2BalanceWithDelegated(BANDWIDTH) / TRX_PRECISION;
        repo.addTotalNetWeight(newNetWeight - oldNetWeight);
        break;
      case ENERGY:
        long oldEnergyWeight = accountCapsule.getFrozenV2BalanceWithDelegated(ENERGY) / TRX_PRECISION;
        accountCapsule.addFrozenBalanceForEnergyV2(-unfreezeBalance);
        long newEnergyWeight = accountCapsule.getFrozenV2BalanceWithDelegated(ENERGY) / TRX_PRECISION;
        repo.addTotalEnergyWeight(newEnergyWeight - oldEnergyWeight);
        break;
      case TRON_POWER:
        long oldTPWeight = accountCapsule.getTronPowerFrozenV2Balance() / TRX_PRECISION;
        accountCapsule.addFrozenForTronPowerV2(-unfreezeBalance);
        long newTPWeight = accountCapsule.getTronPowerFrozenV2Balance() / TRX_PRECISION;
        repo.addTotalTronPowerWeight(newTPWeight - oldTPWeight);
        break;
      default:
        //this should never happen
        break;
    }
```

**File:** chainbase/src/main/java/org/tron/core/db/BandwidthProcessor.java (L432-466)
```java
  public long calculateGlobalNetLimit(AccountCapsule accountCapsule) {
    long frozeBalance = accountCapsule.getAllFrozenBalanceForBandwidth();
    if (dynamicPropertiesStore.supportUnfreezeDelay()) {
      return calculateGlobalNetLimitV2(frozeBalance);
    }
    if (frozeBalance < TRX_PRECISION) {
      return 0;
    }
    long totalNetLimit = chainBaseManager.getDynamicPropertiesStore().getTotalNetLimit();
    long totalNetWeight = chainBaseManager.getDynamicPropertiesStore().getTotalNetWeight();
    if (dynamicPropertiesStore.allowNewReward() && totalNetWeight <= 0) {
      return 0;
    }
    if (totalNetWeight == 0) {
      return 0;
    }
    if (hardenCalculation()) {
      return calculateGlobalLimitV1(frozeBalance, totalNetLimit, totalNetWeight);
    }
    long netWeight = frozeBalance / TRX_PRECISION;
    return (long) (netWeight * ((double) totalNetLimit / totalNetWeight));
  }

  public long calculateGlobalNetLimitV2(long frozeBalance) {
    long totalNetLimit = dynamicPropertiesStore.getTotalNetLimit();
    long totalNetWeight = dynamicPropertiesStore.getTotalNetWeight();
    if (totalNetWeight == 0) {
      return 0;
    }
    if (hardenCalculation()) {
      return calculateGlobalLimitV2(frozeBalance, totalNetLimit, totalNetWeight);
    }
    double netWeight = (double) frozeBalance / TRX_PRECISION;
    return (long) (netWeight * ((double) totalNetLimit / totalNetWeight));
  }
```
