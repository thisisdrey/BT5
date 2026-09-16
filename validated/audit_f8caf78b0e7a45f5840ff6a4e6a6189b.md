### Title
Missing runtime guard against `totalEnergyWeight == 0` in `RepositoryImpl.calculateGlobalEnergyLimit` can throw an uncaught `ArithmeticException` during TVM contract calls - (File: actuator/src/main/java/org/tron/core/vm/repository/RepositoryImpl.java)

### Summary
`RepositoryImpl.calculateGlobalEnergyLimit(AccountCapsule)` divides by `totalEnergyWeight` (the network-wide sum of frozen-for-energy balances) but only guards against this value being zero with a Java `assert` statement, not a real `if` check. Since JVM assertions are disabled by default (no `-ea`/`enableassertions` flag is set anywhere in this codebase's configuration or launch scripts), the `assert` is a no-op in production, leaving the subsequent `BigInteger` division completely unguarded.

### Finding Description
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
``` [1](#0-0) 

This is the exact bug class described in the external report: a value (`totalEnergyWeight`, analogous to CvgRewards' `_totalWeight`) can become `0` and is used as a divisor without a real guard, only an `assert` that evaporates in production JVMs. Note that other implementations of the same logic in this codebase *do* use proper `if` guards, showing developers were aware of the zero-weight case but missed it here:
- `EnergyProcessor.calculateGlobalEnergyLimit` checks `if (dynamicPropertiesStore.allowNewReward() && totalEnergyWeight <= 0) { return 0; } else { assert totalEnergyWeight > 0; }` [2](#0-1) 
- `EnergyProcessor.calculateGlobalEnergyLimitV2` checks `if (totalEnergyWeight == 0) { return 0; }` before dividing [3](#0-2) 
- `BandwidthProcessor.calculateGlobalNetLimit` / `calculateGlobalNetLimitV2` also check `totalNetWeight == 0` before dividing [4](#0-3) 

The `RepositoryImpl` version, used specifically in the TVM execution path, has none of these real checks — only the ineffective `assert`.

When `hardenResourceCalculation()` (i.e. `allowHardenResourceCalculation` chain parameter) is enabled and `totalEnergyWeight` is `0`, `BigInteger.divide(BigInteger.ZERO)` throws `java.lang.ArithmeticException: BigInteger divide by zero`. This method is reachable from ordinary contract execution: `RepositoryImpl.calculateGlobalEnergyLimit` is called by `VMActuator.getAccountEnergyLimitWithFloatRatio` [5](#0-4)  during every TVM call that computes the caller's available energy from frozen-for-energy balance, and is also exposed to contract code itself through the `Repository`/`ContractState` delegation chain [6](#0-5) .

### Impact Explanation
If `totalEnergyWeight` (the dynamic-properties-store-tracked sum of all frozen-for-energy weight across the network) is `0` — e.g., on a private/test chain, freshly bootstrapped network, or any network state where all energy freezes have been withdrawn — any account with `getAllFrozenBalanceForEnergy() >= TRX_PRECISION` invoking or being invoked in a smart-contract call will trigger this code path. The uncaught `ArithmeticException` propagates out of energy-limit calculation during transaction processing, which can crash or halt block/transaction processing for any node that executes such a transaction, denying service to that specific execution path. Because the guard is entirely dependent on an `assert` that does nothing at runtime, this is a genuine unguarded divide-by-zero, not a documented invariant enforced elsewhere.

### Likelihood Explanation
This requires `totalEnergyWeight == 0` while the `allowHardenResourceCalculation` parameter is active, and requires an account with a small nonzero frozen-for-energy balance to trigger the affected code path via any TVM contract call (`TriggerSmartContract`). While mainnet with an established validator set is unlikely to have `totalEnergyWeight == 0` under normal operation, it is a realistic state on private/test networks or shortly after network initialization, and the missing guard represents a genuine defect relative to the parallel, correctly-guarded implementations in `EnergyProcessor`/`BandwidthProcessor` in the same codebase.

### Recommendation
Replace the `assert totalEnergyWeight > 0;` in `RepositoryImpl.calculateGlobalEnergyLimit` with a real runtime check consistent with `EnergyProcessor`'s guarded implementations, e.g.:
```java
if (totalEnergyWeight <= 0) {
  return 0;
}
```
before entering either the `hardenResourceCalculation()` `BigInteger` branch or the `double` fallback branch.

### Proof of Concept
1. Deploy or operate a chain state where `DynamicPropertiesStore.getTotalEnergyWeight()` returns `0` (e.g., fresh/private network before any energy freeze, or after all frozen-for-energy balances have been fully unfrozen).
2. Enable the `allowHardenResourceCalculation` chain parameter (`hardenResourceCalculation()` returns `true`).
3. From any account with `getAllFrozenBalanceForEnergy() >= TRX_PRECISION` (1 TRX), submit a `TriggerSmartContract` transaction that invokes any contract, causing `VMActuator` to call `getAccountEnergyLimitWithFloatRatio` → `rootRepository.calculateGlobalEnergyLimit(account)` [7](#0-6) .
4. Observe `BigInteger.valueOf(totalEnergyLimit).divide(BigInteger.valueOf(0))` throw `ArithmeticException` inside `RepositoryImpl.calculateGlobalEnergyLimit` [8](#0-7) , which is uncaught along this call path, disrupting transaction/block processing.

### Citations

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

**File:** chainbase/src/main/java/org/tron/core/db/EnergyProcessor.java (L154-160)
```java
    long totalEnergyLimit = dynamicPropertiesStore.getTotalEnergyCurrentLimit();
    long totalEnergyWeight = dynamicPropertiesStore.getTotalEnergyWeight();
    if (dynamicPropertiesStore.allowNewReward() && totalEnergyWeight <= 0) {
      return 0;
    } else {
      assert totalEnergyWeight > 0;
    }
```

**File:** chainbase/src/main/java/org/tron/core/db/EnergyProcessor.java (L168-179)
```java
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

**File:** actuator/src/main/java/org/tron/core/actuator/VMActuator.java (L639-663)
```java
    long energyFromFeeLimit;
    long totalBalanceForEnergyFreeze = account.getAllFrozenBalanceForEnergy();
    if (0 == totalBalanceForEnergyFreeze) {
      energyFromFeeLimit =
          feeLimit / sunPerEnergy;
    } else {
      long totalEnergyFromFreeze = rootRepository
          .calculateGlobalEnergyLimit(account);
      long leftBalanceForEnergyFreeze = getEnergyFee(totalBalanceForEnergyFreeze,
          leftEnergyFromFreeze,
          totalEnergyFromFreeze);

      if (leftBalanceForEnergyFreeze >= feeLimit) {
        energyFromFeeLimit = BigInteger.valueOf(totalEnergyFromFreeze)
            .multiply(BigInteger.valueOf(feeLimit))
            .divide(BigInteger.valueOf(totalBalanceForEnergyFreeze)).longValueExact();
      } else {
        energyFromFeeLimit = addExact(
            leftEnergyFromFreeze, (feeLimit - leftBalanceForEnergyFreeze) / sunPerEnergy,
            VMConfig.disableJavaLangMath());
      }
    }

    return min(addExact(leftEnergyFromFreeze, energyFromBalance,
            VMConfig.disableJavaLangMath()), energyFromFeeLimit, VMConfig.disableJavaLangMath());
```

**File:** actuator/src/main/java/org/tron/core/vm/program/ContractState.java (L287-290)
```java
  @Override
  public long calculateGlobalEnergyLimit(AccountCapsule accountCapsule) {
    return repository.calculateGlobalEnergyLimit(accountCapsule);
  }
```
