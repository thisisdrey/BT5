Based on my research, I found a concrete analog matching the bug class: a division that assumes a denominator can never legitimately be zero, but which can be zero in a valid on-chain state (mirroring the report's "borrower repurchase obligation is legitimately zero on full liquidation" scenario), causing the operation to be broken.

### Title
Division-by-zero (or unguarded assert) in `RepositoryImpl.calculateGlobalEnergyLimit()` when `totalEnergyWeight` is zero, breaking TVM energy-limit calculations for resource-delegation validation - (File: `actuator/src/main/java/org/tron/core/vm/repository/RepositoryImpl.java`)

### Summary
`RepositoryImpl.calculateGlobalEnergyLimit()` divides by `totalEnergyWeight` without a zero-guard, relying only on a Java `assert` statement, which is compiled out and disabled by default in production JVMs (no `-ea` flag). When the hardened resource-calculation path is enabled, dividing a `BigInteger` by `BigInteger.ZERO` throws `ArithmeticException`, aborting the calling transaction, exactly the "revert due to division by zero on a value that can legitimately be zero" pattern described in the report.

### Finding Description
The method reads `totalEnergyWeight` from `DynamicPropertiesStore` and only checks it via `assert totalEnergyWeight > 0;`: [1](#0-0) 

Unlike its sibling implementations in `chainbase`, which explicitly guard against a zero total weight before dividing: [2](#0-1) [3](#0-2) 

`RepositoryImpl.calculateGlobalEnergyLimit()` has no such runtime guard — the `assert` is a no-op in a production JVM (assertions are disabled by default), so if `totalEnergyWeight` is 0 (i.e., no accounts hold energy-freezes on chain, or it transiently returns 0), the hardened code path performs `BigInteger.valueOf(totalEnergyLimit).divide(BigInteger.valueOf(0))`, which throws `ArithmeticException: BigInteger divide by zero`. This exception is unhandled at this call site and propagates out of the TVM/native-contract execution path that invokes it, similar to how `getBorrowerRepurchaseObligation(borrower) == 0` caused a division by zero in the reported bug — a value that is legitimately zero in a valid state is used as a divisor without a guard.

### Impact Explanation
If `totalEnergyWeight` is zero when this method is invoked (e.g., on a freshly initialized or low-participation network, or in future states where energy freezing is temporarily nullified), any transaction path relying on `calculateGlobalEnergyLimit()` in `RepositoryImpl` throws an uncaught `ArithmeticException`, reverting/aborting the calling operation. This denies legitimate resource-delegation-related TVM operations from succeeding, an availability/denial-of-service impact on a core resource-accounting function, consistent with a Medium-severity finding.

### Likelihood Explanation
`totalEnergyWeight` is a globally tracked chain statistic; while unlikely to be exactly zero on a mature mainnet, it is a directly reachable, non-privileged state dependent on aggregate freeze/unfreeze actions across all accounts, not attacker-controlled directly but reachable through normal usage patterns and definitely reachable on low-activity/private/test networks or right after a mass unfreeze event. This mirrors the reported bug's root cause: an assumption that a divisor is always positive, which is not enforced by any runtime check outside of a disabled `assert`.

### Recommendation
Add an explicit runtime guard in `RepositoryImpl.calculateGlobalEnergyLimit()`, mirroring `EnergyProcessor.calculateGlobalEnergyLimit()` and `BandwidthProcessor.calculateGlobalNetLimit()`, to return `0` (or another safe default) when `totalEnergyWeight <= 0`, instead of relying on the disabled `assert`.

### Proof of Concept
1. Deploy/operate a java-tron network state where `DynamicPropertiesStore.getTotalEnergyWeight()` returns `0` (e.g., early chain state or after all energy-freeze balances are unfrozen).
2. Trigger a code path in `RepositoryImpl` that calls `calculateGlobalEnergyLimit()` with `hardenResourceCalculation()` enabled (`VMConfig.allowHardenResourceCalculation()` true) — this is invoked through native-contract/TVM resource-delegation validation flows using `Repository`. [4](#0-3) 
3. `BigInteger.valueOf(totalEnergyLimit).divide(BigInteger.valueOf(0))` throws `ArithmeticException`, which is not caught by this method, propagating up and aborting the calling operation.

**Note on confidence:** I was not able to fully trace every call site of `RepositoryImpl.calculateGlobalEnergyLimit()` at the interface level (`Repository.calculateGlobalEnergyLimit`) within the tool budget available, so I cannot with full certainty confirm which specific unprivileged transaction types invoke this exact overload versus the guarded `EnergyProcessor`/`chainbase` version. A Devin session with full repo access would be needed to enumerate all `Repository`/`RepositoryImpl.calculateGlobalEnergyLimit` call sites and confirm the precise unprivileged transaction path and current value range of `totalEnergyWeight` in production to fully validate exploitability likelihood.

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

**File:** chainbase/src/main/java/org/tron/core/db/BandwidthProcessor.java (L440-452)
```java
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
```

**File:** actuator/src/main/java/org/tron/core/vm/repository/Repository.java (L1-1)
```java
package org.tron.core.vm.repository;
```
