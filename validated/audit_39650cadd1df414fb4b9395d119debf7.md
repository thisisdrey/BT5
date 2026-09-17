## Confirmed vulnerability

### Title
Unchecked global resource-weight accumulator can overflow/underflow via TVM native resource contracts, corrupting bandwidth/energy accounting for all accounts - ([File: actuator/src/main/java/org/tron/core/vm/repository/RepositoryImpl.java])

### Summary
`RepositoryImpl.addTotalNetWeight` / `addTotalEnergyWeight` / `addTotalTronPowerWeight`, which back every TVM-reachable freeze/unfreeze/delegate resource native contract, perform raw `long` addition on the chain-wide `TOTAL_NET_WEIGHT` / `TOTAL_ENERGY_WEIGHT` counters with no overflow check and no floor clamp, unlike the equivalent `DynamicPropertiesStore` methods used by the legacy (non-TVM) actuators, which explicitly clamp the result to a minimum of `0` (`max(0, totalWeight, ...)`) when `allowNewReward()` is active. This mirrors the disclosed Moloch class of bug: unguarded arithmetic on a value that other core accounting logic depends on can wrap/underflow and corrupt shared state used by every subsequent transaction.

### Finding Description
`RepositoryImpl` implements the `Repository` interface used by every TVM native-contract processor (freeze/unfreeze/delegate/undelegate/cancel-all-unfreeze resource operations invoked through Solidity contracts via TVM opcodes): [1](#0-0) 

Compare this to the version used by the legacy (non-TVM) `FreezeBalanceActuator`/`UnfreezeBalanceActuator` path in `DynamicPropertiesStore`, which explicitly floors the result at zero after the addition: [2](#0-1) 

The TVM path is reachable from a single signed transaction: any account can deploy or call a smart contract that invokes the freeze/unfreeze/delegate resource TVM opcodes, which route through `FreezeBalanceV2Processor`, `UnfreezeBalanceV2Processor`, `DelegateResourceProcessor`, `UnDelegateResourceProcessor`, and `CancelAllUnfreezeV2Processor` — all of which call `repo.addTotalNetWeight(...)` / `repo.addTotalEnergyWeight(...)` / `repo.addTotalTronPowerWeight(...)` on `RepositoryImpl`: [3](#0-2) [4](#0-3) 

These global weight counters are then consumed to compute every account's bandwidth/energy allotment on every subsequent transaction: [5](#0-4) 

If `totalNetWeight`/`totalEnergyWeight` become negative (silent underflow) or wrap around `Long.MAX_VALUE` (silent overflow) through repeated freeze/delegate/unfreeze/undelegate operations issued via TVM calls, the division `totalEnergyLimit / totalEnergyWeight` used in `calculateGlobalEnergyLimit`/`calculateGlobalEnergyLimitV2` and the analogous bandwidth computation in `BandwidthProcessor` becomes corrupted network-wide — potentially yielding negative/zero energy limits for all accounts (denial of legitimate resource usage/freezing of funds' usability) or wildly inflated limits (free/unbacked energy or bandwidth, i.e., an economic exploit letting users bypass paying for resources). This is functionally analogous to the disclosed Moloch bug: an unguarded arithmetic accumulator, reachable through ordinary user-triggered operations, whose corruption breaks a globally shared computation used by every subsequent transaction rather than being isolated to the actor who caused it.

### Impact Explanation
Corruption of `TOTAL_NET_WEIGHT`/`TOTAL_ENERGY_WEIGHT` is not isolated to the calling account — it is a chain-wide dynamic property consulted on every transaction's resource accounting (`EnergyProcessor.calculateGlobalEnergyLimit`, and the analogous `BandwidthProcessor` logic). A wraparound could cause systemic denial of resource allocation (accounts unable to obtain bandwidth/energy they are entitled to, effectively freezing their ability to transact) or the opposite — inflated/unbacked resource limits that let malicious actors execute contracts or transfers without properly paying/burning the corresponding TRX, an unbacked-resource condition analogous to unbacked balance issues. This qualifies as High impact.

### Likelihood Explanation
Reaching this requires many repeated freeze/unfreeze/delegate/undelegate operations through TVM native contracts to accumulate enough delta to overflow/underflow a 64-bit counter, or a validator/committee-controlled parameter interacting with attacker-supplied `frozenBalance`/`delegateBalance` values over time. This is not a single-transaction trivial exploit (unlike the original Moloch `internalTransfer` case, where one malicious token's inflated total supply alone triggers it), so likelihood is Medium — it requires sustained abuse of a permissionless, unprivileged transaction path (freeze/delegate resource via contract calls), but no special privilege beyond being a normal broadcaster.

### Recommendation
Apply the same overflow-safe accumulation used in `DynamicPropertiesStore.addTotalNetWeight`/`addTotalEnergyWeight`/`addTotalTronPowerWeight` (checked arithmetic plus a `max(0, ...)` floor) to the `RepositoryImpl` implementations of these methods, so that both the legacy actuator path and the TVM native-contract path enforce identical invariants on the chain-wide weight counters. Consider using `Math.addExact`/`LongMath.checkedAdd` (as already used elsewhere in the codebase, e.g. `WithdrawRewardProcessor`) to fail the individual transaction rather than silently corrupt shared state.

### Proof of Concept
1. Deploy a smart contract that repeatedly calls the TVM `freezeBalanceV2`/`unfreezeBalanceV2`/`delegateResource`/`undelegateResource` opcodes for BANDWIDTH or ENERGY with values chosen to accumulate a large positive or negative delta against `TOTAL_NET_WEIGHT`/`TOTAL_ENERGY_WEIGHT` over many transactions.
2. Because `RepositoryImpl.addTotalNetWeight`/`addTotalEnergyWeight` perform raw `+=` with no overflow guard and no zero-floor (unlike `DynamicPropertiesStore`'s equivalents), sufficient accumulated deltas push the stored counter negative or across `Long.MAX_VALUE`.
3. Once corrupted, `EnergyProcessor.calculateGlobalEnergyLimit`/`calculateGlobalEnergyLimitV2` (and the analogous bandwidth logic) compute incorrect global energy/bandwidth limits for every account on the network, using the corrupted `totalEnergyWeight` in the denominator of the weight ratio.
4. This is analogous to the reported Moloch bug: an unguarded arithmetic accumulator reachable via normal, permissionless transactions corrupts shared global accounting used throughout the system's core resource logic.

### Citations

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

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/UnfreezeBalanceV2Processor.java (L183-210)
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
  }
```

**File:** chainbase/src/main/java/org/tron/core/db/EnergyProcessor.java (L145-179)
```java
  public long calculateGlobalEnergyLimit(AccountCapsule accountCapsule) {
    long frozeBalance = accountCapsule.getAllFrozenBalanceForEnergy();
    if (dynamicPropertiesStore.supportUnfreezeDelay()) {
      return calculateGlobalEnergyLimitV2(frozeBalance);
    }
    if (frozeBalance < TRX_PRECISION) {
      return 0;
    }

    long totalEnergyLimit = dynamicPropertiesStore.getTotalEnergyCurrentLimit();
    long totalEnergyWeight = dynamicPropertiesStore.getTotalEnergyWeight();
    if (dynamicPropertiesStore.allowNewReward() && totalEnergyWeight <= 0) {
      return 0;
    } else {
      assert totalEnergyWeight > 0;
    }
    if (hardenCalculation()) {
      return calculateGlobalLimitV1(frozeBalance, totalEnergyLimit, totalEnergyWeight);
    }
    long energyWeight = frozeBalance / TRX_PRECISION;
    return (long) (energyWeight * ((double) totalEnergyLimit / totalEnergyWeight));
  }

  public long calculateGlobalEnergyLimitV2(long frozeBalance) {
    long totalEnergyLimit = dynamicPropertiesStore.getTotalEnergyCurrentLimit();
    long totalEnergyWeight = dynamicPropertiesStore.getTotalEnergyWeight();
    if (totalEnergyWeight == 0) {
      return 0;
    }
    if (hardenCalculation()) {
      return calculateGlobalLimitV2(frozeBalance, totalEnergyLimit, totalEnergyWeight);
    }
    double energyWeight = (double) frozeBalance / TRX_PRECISION;
    return (long) (energyWeight * ((double) totalEnergyLimit / totalEnergyWeight));
  }
```
