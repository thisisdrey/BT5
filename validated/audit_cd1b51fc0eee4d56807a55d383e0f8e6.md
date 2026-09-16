### Title
Divide-by-zero in `RepositoryImpl.calculateGlobalEnergyLimit` due to disabled `assert` guard on `totalEnergyWeight` - (File: actuator/src/main/java/org/tron/core/vm/repository/RepositoryImpl.java)

### Summary
`RepositoryImpl.calculateGlobalEnergyLimit(AccountCapsule)`, the `Repository`-facing implementation used by TVM contract execution (exposed via `ContractState.calculateGlobalEnergyLimit`), divides by `totalEnergyWeight` (`dynamicPropertiesStore.getTotalEnergyWeight()`) with only an `assert` as a safety check, rather than an explicit runtime guard.

### Finding Description
The method reads: [1](#0-0) 

Specifically:
```
long totalEnergyWeight = getDynamicPropertiesStore().getTotalEnergyWeight();
assert totalEnergyWeight > 0;
if (hardenResourceCalculation()) {
  return BigInteger.valueOf(energyWeight).multiply(...).divide(BigInteger.valueOf(totalEnergyWeight)).longValueExact();
}
return (long) (energyWeight * ((double) totalEnergyLimit / totalEnergyWeight));
```
`assert` statements are compiled out / disabled by default in production JVMs (no `-ea` flag), so `assert totalEnergyWeight > 0;` provides **no actual protection** at runtime. If `totalEnergyWeight` is `0`, the hardened `BigInteger` path throws `ArithmeticException: BigInteger divide by zero`, an uncaught runtime exception during TVM execution.

Compare this to the equivalent, actually-hardened production paths in `EnergyProcessor.calculateGlobalEnergyLimit`/`calculateGlobalEnergyLimitV2` and `BandwidthProcessor.calculateGlobalNetLimit`/`calculateGlobalNetLimitV2`, which contain real runtime checks (`if (totalEnergyWeight <= 0) return 0;` / `if (totalNetWeight == 0) return 0;`) before dividing: [2](#0-1) [3](#0-2) 

`RepositoryImpl.calculateGlobalEnergyLimit` lacks this same explicit `if (totalEnergyWeight == 0) return 0;` check — it is missing the exact guard present in its sibling classes, which is the root cause.

This method is part of the `Repository` interface implemented by `RepositoryImpl` and delegated through `ContractState` (the repository object visible to executing TVM contracts): [4](#0-3) 

### Impact Explanation
If `totalEnergyWeight` (a `DynamicPropertiesStore` value tracking total frozen-for-energy balance network-wide) is `0` and `allowHardenResourceCalculation` is enabled, any call into this code path throws an unhandled `ArithmeticException`. Depending on where this propagates in the TVM execution/actuator pipeline, this can crash block processing (denial-of-service / node halt) rather than being caught and converted into a normal contract revert, since it is an unexpected runtime exception rather than a `ContractValidateException`/`ContractExeException`.

### Likelihood Explanation
Exploitation requires `totalEnergyWeight == 0` while `hardenResourceCalculation()` is enabled — a state that should not normally occur on an established mainnet with active energy freezes, but is plausible on a fresh/private chain or if all energy-freezing accounts have fully unfrozen (post `supportUnfreezeDelay`/`UnfreezeBalanceV2` operations), which are all normal user-triggered actions. Because the safety net is an `assert` (disabled by default), there is no actual runtime protection once that state is reached; this contrasts with the parallel, safe implementations in `EnergyProcessor`/`BandwidthProcessor`, showing the fix is straightforward and was clearly intended but is missing in `RepositoryImpl`. I could not fully verify from the available index which specific TVM opcode/precompiled contract calls trigger `RepositoryImpl.calculateGlobalEnergyLimit` at runtime (only `ContractState`'s delegate and a reference in `VMActuator.java` were found by grep, but their exact call sites were not retrievable in this session).

### Recommendation
Add the same explicit guard used in `EnergyProcessor`/`BandwidthProcessor` to `RepositoryImpl.calculateGlobalEnergyLimit`:
```java
long totalEnergyWeight = getDynamicPropertiesStore().getTotalEnergyWeight();
if (totalEnergyWeight <= 0) {
  return 0;
}
```
before either the `BigInteger` or `double` division, removing reliance on the disabled `assert`.

### Proof of Concept
1. Configure/reach a chain state where `DynamicPropertiesStore.getTotalEnergyWeight()` returns `0` (e.g., a fresh chain/testnet before any account freezes TRX for energy, or after all energy-frozen balances are fully withdrawn) and `allowHardenResourceCalculation` is enabled.
2. Trigger any code path that invokes `Repository.calculateGlobalEnergyLimit(accountCapsule)` on an account with `getAllFrozenBalanceForEnergy() >= TRX_PRECISION` (e.g., via a TVM contract call exercising this repository method through `ContractState`).
3. Execution reaches `BigInteger.valueOf(energyWeight).multiply(BigInteger.valueOf(totalEnergyLimit)).divide(BigInteger.valueOf(0))`, throwing `ArithmeticException: BigInteger divide by zero`, since the preceding `assert totalEnergyWeight > 0;` is a no-op in production (assertions disabled by default).

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

**File:** chainbase/src/main/java/org/tron/core/db/EnergyProcessor.java (L154-166)
```java
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

**File:** chainbase/src/main/java/org/tron/core/db/BandwidthProcessor.java (L440-447)
```java
    long totalNetLimit = chainBaseManager.getDynamicPropertiesStore().getTotalNetLimit();
    long totalNetWeight = chainBaseManager.getDynamicPropertiesStore().getTotalNetWeight();
    if (dynamicPropertiesStore.allowNewReward() && totalNetWeight <= 0) {
      return 0;
    }
    if (totalNetWeight == 0) {
      return 0;
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/program/ContractState.java (L287-290)
```java
  @Override
  public long calculateGlobalEnergyLimit(AccountCapsule accountCapsule) {
    return repository.calculateGlobalEnergyLimit(accountCapsule);
  }
```
