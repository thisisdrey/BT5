## Analysis

The Sherlock report describes a bug class where a party who can invoke `stake()`/`withdraw()` can push `_totalSupply` in `VirtualStakingRewards` to values that break the `rewardPerToken()` division, because the arithmetic that updates the aggregate denominator has no floor/overflow protection.

The java-tron analog for this class ("stake/delegation/reward math ... reachable by an unprivileged transaction broadcaster") is the **global resource-weight accounting used to compute per-account bandwidth/energy limits**, specifically the divergence between two implementations of the same accounting method:

- The legacy, non-TVM path in `DynamicPropertiesStore.addTotalNetWeight` / `addTotalEnergyWeight` / `addTotalTronPowerWeight` clamps the running total to a floor of `0` whenever `allowNewReward()` is enabled: [1](#0-0) 

- The TVM-reachable path, `RepositoryImpl.addTotalNetWeight` / `addTotalEnergyWeight` / `addTotalTronPowerWeight`, used by every native-contract processor invoked through TVM opcodes (`freezeBalanceV2`, `unfreezeBalanceV2`, `delegateResource`, `unDelegateResource`), has **no such clamp at all**: [2](#0-1) 

These weight totals are used directly as the divisor in the global resource-limit formulas: [3](#0-2) [4](#0-3) 

Note the `assert totalEnergyWeight > 0;` on line 1001/159 — Java assertions are disabled by default in production JVMs (no `-ea` flag), so this provides **no actual runtime protection** against a zero or negative divisor reaching the division. This is directly analogous to the reported issue: a globally-shared accounting denominator, reachable and mutable through ordinary user-facing operations (freeze/unfreeze/delegate via TVM), that feeds a division used to compute value distribution (bandwidth/energy allotment) for every account on the network, without the safety clamp its sibling implementation has.

### Title
Unclamped Global Resource-Weight Accounting in TVM Native-Contract Path Enables Division-by-Zero/Negative Divisor - ([File: actuator/src/main/java/org/tron/core/vm/repository/RepositoryImpl.java])

### Summary
`RepositoryImpl.addTotalNetWeight`, `addTotalEnergyWeight`, and `addTotalTronPowerWeight` — the accounting entry points used by all TVM-reachable freeze/unfreeze/delegate native contracts — update the global `TOTAL_NET_WEIGHT` / `TOTAL_ENERGY_WEIGHT` / `TOTAL_TRON_POWER_WEIGHT` counters with plain unclamped addition, unlike the parallel, hardened implementation in `DynamicPropertiesStore` used by the legacy (non-VM) actuators, which floors the result at `0` whenever `allowNewReward()` is active.

### Finding Description
`FreezeBalanceV2Processor`, `UnfreezeBalanceV2Processor`, `DelegateResourceProcessor`, and `UnDelegateResourceProcessor` are executed from TVM opcodes (`Program.freezeBalanceV2`, `unfreezeBalanceV2`, `delegateResource`, etc.), reachable by any deployed contract triggered from an ordinary, unprivileged transaction. These processors call `repo.addTotalNetWeight(...)` / `repo.addTotalEnergyWeight(...)` / `repo.addTotalTronPowerWeight(...)`: [5](#0-4) [6](#0-5) 

`RepositoryImpl`'s implementation of these setters performs a bare `+=` with **no floor/negative check**: [2](#0-1) 

whereas `DynamicPropertiesStore`'s implementation — used by legacy, non-TVM `FreezeBalanceActuator`/`UnfreezeBalanceActuator` — explicitly floors the accumulated weight to zero when the new-reward algorithm is enabled: [1](#0-0) 

These global weight values are then consumed as the divisor of the resource-limit formula that governs every account's bandwidth/energy allocation: [3](#0-2) [4](#0-3) 

The only guard against a zero/negative divisor in the `RepositoryImpl` path is a Java `assert`, which is compiled out and disabled at runtime by default (`assert totalEnergyWeight > 0;`), so it never actually throws in production. If repeated freeze/unfreeze/delegate cycles (whose per-account weight deltas are computed via `TRX_PRECISION`-truncated integer division, e.g. `getFrozenV2BalanceWithDelegated(...) / TRX_PRECISION`) accumulate rounding drift across many accounts and TVM-triggered operations, the global `totalNetWeight`/`totalEnergyWeight` tracked via the unclamped `RepositoryImpl` path can drift to zero or below — a state the parallel, non-TVM path explicitly prevents.

### Impact Explanation
If the global weight denominator reaches `0`, `calculateGlobalEnergyLimit`/`calculateGlobalNetLimit`/`calculateGlobalEnergyLimitV2`/`calculateGlobalNetLimitV2` divide by zero, throwing `ArithmeticException` inside TVM execution paths that are invoked as part of ordinary block processing — this can disrupt resource accounting for the entire node for every account, not just the caller. If it goes negative instead, the resulting global bandwidth/energy limit computation becomes corrupted network-wide (e.g., negative or wildly incorrect limits), which is a shared, global accounting variable, so the effect is not confined to the attacker's own account — it can degrade or corrupt resource issuance and consumption checks for arbitrary accounts across the chain.

### Likelihood Explanation
Exploitation requires a deployed contract driving many freeze/unfreeze/delegate cycles through the TVM opcodes to accumulate enough truncation drift to zero-out (or flip negative) a large, network-wide counter, so the practical likelihood is inherently constrained by the sheer scale of TRX staked across the network. This bounds the severity/likelihood relative to a single-account, directly-controllable overflow, but the structural inconsistency itself — an intentionally-added safety clamp in one implementation that is silently missing in the parallel TVM-facing implementation of the identical accounting function — is a genuine, unprivileged-reachable root cause without any working runtime guard.

### Recommendation
Apply the same `max(0, totalWeight, disableJavaLangMath())` floor (mirroring `DynamicPropertiesStore.addTotalNetWeight`/`addTotalEnergyWeight`/`addTotalTronPowerWeight`) inside `RepositoryImpl.addTotalNetWeight`, `addTotalEnergyWeight`, and `addTotalTronPowerWeight`, and replace the runtime-disabled `assert totalEnergyWeight > 0;` checks in `RepositoryImpl.calculateGlobalEnergyLimit` (and the equivalent bandwidth calculation) with explicit `if (totalWeight <= 0) return 0;` guards, consistent with `EnergyProcessor.calculateGlobalEnergyLimit`'s `allowNewReward()` check.

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

**File:** actuator/src/main/java/org/tron/core/vm/repository/RepositoryImpl.java (L992-1010)
```java
  public long calculateGlobalEnergyLimit(AccountCapsule accountCapsule) {
    long frozeBalance = accountCapsule.getAllFrozenBalanceForEnergy();
    if (frozeBalance < TRX_PRECISION) {
      return 0;
    }
    long energyWeight = frozeBalance / TRX_PRECISION;
    long totalEnergyLimit = getDynamicPropertiesStore().getTotalEnergyCurrentLimit();
    long totalEnergyWeight = getDynamicPropertiesStore().getTotalEnergyWeight();

    assert totalEnergyWeight > 0;

    if (hardenResourceCalculation()) {
      return BigInteger.valueOf(energyWeight)
          .multiply(BigInteger.valueOf(totalEnergyLimit))
          .divide(BigInteger.valueOf(totalEnergyWeight))
          .longValueExact();
    }
    return (long) (energyWeight * ((double) totalEnergyLimit / totalEnergyWeight));
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

**File:** chainbase/src/main/java/org/tron/core/db/EnergyProcessor.java (L145-166)
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
