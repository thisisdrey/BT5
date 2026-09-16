### Title
Divide-by-zero (BigInteger `ArithmeticException`) in `RepositoryImpl.calculateGlobalEnergyLimit` when hardened resource calculation is enabled and `TOTAL_ENERGY_WEIGHT` is zero - (File: actuator/src/main/java/org/tron/core/vm/repository/RepositoryImpl.java)

### Summary
`RepositoryImpl.calculateGlobalEnergyLimit` divides by `totalEnergyWeight` fetched from the dynamic-properties store, guarded only by a `assert` statement, which is compiled out and disabled by default at runtime (JVM assertions are off unless started with `-ea`). When `VMConfig.allowHardenResourceCalculation()` is on, the divisor is used in a `BigInteger.divide(...)` call, which throws an uncaught `ArithmeticException: BigInteger divide by zero` if `totalEnergyWeight == 0` [1](#0-0) .

### Finding Description
`calculateGlobalEnergyLimit` reads `totalEnergyWeight` via `getDynamicPropertiesStore().getTotalEnergyWeight()` and only "protects" the division with `assert totalEnergyWeight > 0;` [2](#0-1) . Java `assert` statements are stripped from the effective control flow unless the JVM is launched with `-ea`, which is not the default for production deployments, so this check provides no real protection in a standard node.

If `hardenResourceCalculation()` returns true, the method proceeds to:
```
return BigInteger.valueOf(energyWeight)
    .multiply(BigInteger.valueOf(totalEnergyLimit))
    .divide(BigInteger.valueOf(totalEnergyWeight))
    .longValueExact();
``` [3](#0-2) 
If `totalEnergyWeight` is `0`, `BigInteger.divide` throws `ArithmeticException: BigInteger divide by zero`. This is analogous to the reported VLC bug class (an unguarded division producing a runtime FPE/exception on attacker/user-influenceable zero-denominator input), the difference being the crash mechanism (`ArithmeticException` vs SIGFPE) rather than the memory-safety mechanism.

By contrast, the equivalent legacy code path in `EnergyProcessor.calculateGlobalEnergyLimit`/`calculateGlobalEnergyLimitV2` explicitly checks `totalEnergyWeight <= 0` and short-circuits to `0` before doing any division [4](#0-3) , showing the intended safe behavior that `RepositoryImpl`'s VM-facing copy of this logic fails to replicate for the zero-weight case.

`calculateGlobalEnergyLimit` in `RepositoryImpl` is reached from `getAccountLeftEnergyFromFreeze`, which is exposed through the `Repository` interface and its delegate `ContractState` used throughout TVM contract execution to determine the caller's available energy from frozen balance [5](#0-4) [6](#0-5) . This means any `TriggerSmartContract` transaction (i.e., any unprivileged contract call/deploy) that queries or is billed against energy-from-frozen-balance can drive this code path.

`totalEnergyWeight` (`TOTAL_ENERGY_WEIGHT`) is a chain-wide accounting value maintained via freeze/unfreeze actuators as accounts stake/unstake TRX for energy; it is theoretically able to reach zero if, at a given moment, no account has any energy freeze outstanding (e.g., after unfreeze operations reduce the aggregate to 0, particularly plausible on a low-activity/private/test/forked network, or transiently during specific upgrade/migration states).

### Impact Explanation
If `totalEnergyWeight` reaches `0` while `allowHardenResourceCalculation` is enabled, any subsequent smart-contract-related call path that invokes `calculateGlobalEnergyLimit`/`getAccountLeftEnergyFromFreeze` throws an uncaught `ArithmeticException`. Depending on how deep in the VM/actuator execution stack this exception propagates and whether it's caught at a coarser level (e.g., generic `Exception` handling in transaction processing), this can manifest as: repeated failed transaction processing (denial of service for TVM calls energy-accounting), or, if unhandled at the block-application layer, a node crash/halt on a legitimately broadcast, syntactically valid smart contract call. This matches the "node crash or halt" / "API the node can no longer serve" impact classes.

### Likelihood Explanation
Exploitability depends on an application-level precondition (`totalEnergyWeight == 0`) that is not attacker-controlled in one transaction; on a live, heavily-used mainnet-like network with many stakers, this value is unlikely to be zero. However, on private chains, freshly bootstrapped networks, test networks, or edge-case migration windows the value could legitimately be zero, and once it is, exploitation requires only a single ordinary `TriggerSmartContract` call — no special privileges. The severity is elevated because there is no way for an unprivileged account to be sure this precondition doesn't exist, and the "protection" (`assert`) is inert in production builds.

### Recommendation
Replace the `assert totalEnergyWeight > 0;` guard in `RepositoryImpl.calculateGlobalEnergyLimit` with an explicit runtime check, e.g. mirror `EnergyProcessor`'s `if (totalEnergyWeight <= 0) return 0;` behavior, before performing either the BigInteger or double-based division, so the hardened path can never divide by zero regardless of JVM assertion settings.

### Proof of Concept
1. On a test/private network (or by manipulating `DynamicPropertiesStore` state, as done in `CalculateGlobalLimitHardenTest`), drive `TOTAL_ENERGY_WEIGHT` to `0` (e.g., via full unfreeze of all energy-frozen accounts) [7](#0-6) .
2. Enable `allowHardenResourceCalculation` (as demonstrated by test toggling `saveAllowHardenResourceCalculation(1)` in `CalculateGlobalLimitHardenTest.java`) [8](#0-7) .
3. Broadcast any `TriggerSmartContract` transaction from an account with a nonzero frozen-for-energy balance so `getAccountLeftEnergyFromFreeze` → `calculateGlobalEnergyLimit` is invoked.
4. Observe `BigInteger.divide` throw `ArithmeticException: BigInteger divide by zero` at `actuator/src/main/java/org/tron/core/vm/repository/RepositoryImpl.java:1006`, since the `assert` at line 1001 does not fire under default (assertions-disabled) JVM execution.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/repository/RepositoryImpl.java (L186-198)
```java
  public long getAccountLeftEnergyFromFreeze(AccountCapsule accountCapsule) {
    long now = getHeadSlot();

    long energyUsage = accountCapsule.getEnergyUsage();
    long latestConsumeTime = accountCapsule.getAccountResource().getLatestConsumeTimeForEnergy();
    long energyLimit = calculateGlobalEnergyLimit(accountCapsule);

    long windowSize = accountCapsule.getWindowSize(Common.ResourceCode.ENERGY);

    long newEnergyUsage = recover(energyUsage, latestConsumeTime, now, windowSize);

    return max(energyLimit - newEnergyUsage, 0, VMConfig.disableJavaLangMath()); // us
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

**File:** chainbase/src/main/java/org/tron/core/db/EnergyProcessor.java (L156-173)
```java
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
```

**File:** actuator/src/main/java/org/tron/core/vm/program/ContractState.java (L267-290)
```java
  @Override
  public long getAccountLeftEnergyFromFreeze(AccountCapsule accountCapsule) {
    return repository.getAccountLeftEnergyFromFreeze(accountCapsule);
  }

  @Override
  public long getAccountEnergyUsage(AccountCapsule accountCapsule) {
    return repository.getAccountEnergyUsage(accountCapsule);
  }

  @Override
  public Pair<Long, Long> getAccountEnergyUsageBalanceAndRestoreSeconds(AccountCapsule accountCapsule) {
    return repository.getAccountEnergyUsageBalanceAndRestoreSeconds(accountCapsule);
  }

  @Override
  public Pair<Long, Long> getAccountNetUsageBalanceAndRestoreSeconds(AccountCapsule accountCapsule) {
    return repository.getAccountNetUsageBalanceAndRestoreSeconds(accountCapsule);
  }

  @Override
  public long calculateGlobalEnergyLimit(AccountCapsule accountCapsule) {
    return repository.calculateGlobalEnergyLimit(accountCapsule);
  }
```

**File:** chainbase/src/main/java/org/tron/core/store/DynamicPropertiesStore.java (L1299-1310)
```java
  public void saveTotalEnergyWeight(long totalEnergyWeight) {
    this.put(DynamicResourceProperties.TOTAL_ENERGY_WEIGHT,
        new BytesCapsule(ByteArray.fromLong(totalEnergyWeight)));
  }

  public long getTotalEnergyWeight() {
    return Optional.ofNullable(getUnchecked(DynamicResourceProperties.TOTAL_ENERGY_WEIGHT))
        .map(BytesCapsule::getData)
        .map(ByteArray::toLong)
        .orElseThrow(
            () -> new IllegalArgumentException("not found TOTAL_ENERGY_WEIGHT"));
  }
```

**File:** framework/src/test/java/org/tron/core/db/CalculateGlobalLimitHardenTest.java (L51-65)
```java
  @Test
  public void testGlobalEnergyLimitParity() {
    dbManager.getDynamicPropertiesStore().saveTotalEnergyCurrentLimit(50_000_000_000L);
    dbManager.getDynamicPropertiesStore().saveTotalEnergyWeight(2_000_000_000L);
    ownerCapsule.setFrozenForEnergy(10_000_000_000L, 0L);
    dbManager.getAccountStore().put(ownerCapsule.getAddress().toByteArray(), ownerCapsule);

    dbManager.getDynamicPropertiesStore().saveAllowHardenResourceCalculation(0);
    long resultOld = energyProcessor.calculateGlobalEnergyLimit(ownerCapsule);

    dbManager.getDynamicPropertiesStore().saveAllowHardenResourceCalculation(1);
    long resultNew = energyProcessor.calculateGlobalEnergyLimit(ownerCapsule);

    Assert.assertEquals(resultOld, resultNew);
  }
```
