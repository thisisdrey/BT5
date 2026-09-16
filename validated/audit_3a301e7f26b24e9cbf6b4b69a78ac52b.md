Found `RepositoryImpl.calculateGlobalEnergyLimit` mirrors the CVE's structural bug: a mandatory divisor (`totalEnergyWeight`) is protected only by a Java `assert` statement, which is compiled out and does nothing at runtime unless the JVM is started with `-ea` (not the default in production). If `getTotalEnergyWeight()` returns `0`, `hardenResourceCalculation()`-enabled path calls `BigInteger.divide(BigInteger.ZERO)`, which throws `ArithmeticException`, aborting energy-limit calculation for any TVM contract call.

### Title
Unguarded division by `totalEnergyWeight` relies on disabled `assert`, causing DoS on TVM contract calls when total energy weight is zero - ([File: actuator/src/main/java/org/tron/core/vm/repository/RepositoryImpl.java])

### Summary
`RepositoryImpl.calculateGlobalEnergyLimit` divides by `totalEnergyWeight` (retrieved from `DynamicPropertiesStore.getTotalEnergyWeight()`) and only guards the zero case with a bare `assert totalEnergyWeight > 0;` statement. Java assertions are disabled by default in production JVMs (no `-ea` flag), so this check is a no-op at runtime, exactly the same bug class as CVE-2024-26945 where `cpus_per_iaa` could be `0` and was consumed by a downstream divide without a real runtime guard. [1](#0-0) 

### Finding Description
`calculateGlobalEnergyLimit(AccountCapsule)` is called from `RepositoryImpl` on every TVM smart-contract invocation to compute the energy limit an account can spend, based on `totalEnergyWeight` (a global chain parameter tracking total frozen-for-energy balance across all accounts). The method fetches `totalEnergyWeight` and immediately runs `assert totalEnergyWeight > 0;`, then unconditionally executes: [2](#0-1) 
When hardened resource calculation is enabled (`VMConfig.allowHardenResourceCalculation()`), the divisor is passed straight into `BigInteger.divide()`, which throws `ArithmeticException` on zero; the non-hardened branch performs a double division, which produces `Infinity`/`NaN` rather than throwing, but still yields a corrupted energy-limit value used for subsequent accounting.

Note the sibling code path in `EnergyProcessor.calculateGlobalEnergyLimit` (used outside the VM/actuator context) does have a real, non-assert guard: `if (dynamicPropertiesStore.allowNewReward() && totalEnergyWeight <= 0) { return 0; }` before falling back to the same `assert`. [3](#0-2) 
`RepositoryImpl.calculateGlobalEnergyLimit`, however, has no such real guard at all — it relies solely on the disabled `assert`.

### Impact Explanation
If `totalEnergyWeight` becomes `0` (or negative) — for example transiently at genesis, after all frozen-for-energy stake is unfrozen/undelegated on a private/consortium chain, or via any state-transition bug that resets the weight — every subsequent TVM contract-triggering transaction (`TriggerSmartContract`, contract deployment) that reaches `calculateGlobalEnergyLimit` in the hardened path throws an uncaught `ArithmeticException` inside energy accounting. This is reachable from any unprivileged transaction broadcaster or contract caller and can crash/halt block processing or contract execution entirely, denying the node's ability to serve TVM calls (matches the "node crash or halt" / "API the node can no longer serve" impact criteria).

### Likelihood Explanation
Likelihood depends on whether `totalEnergyWeight` can realistically reach `0` on a live network with active freezing; on Mainnet with existing frozen balances this is unlikely, but on any low-stake, freshly bootstrapped, or private/consortium java-tron network (a stated concern the assert itself acknowledges) it is plausible, and the guard that should prevent it is compiled away in every standard production JVM. Because the fix depends on JVM assertion flags rather than actual code, the protection is effectively absent by default.

### Recommendation
Replace the `assert totalEnergyWeight > 0;` in `RepositoryImpl.calculateGlobalEnergyLimit` with an explicit runtime check mirroring `EnergyProcessor.calculateGlobalEnergyLimitV2`'s pattern (`if (totalEnergyWeight <= 0) { return 0; }`) before performing any division, so the divisor can never be zero regardless of JVM assertion settings.

### Proof of Concept
1. On a test/private java-tron network, drive `totalEnergyWeight` to `0` by unfreezing/undelegating all energy-frozen balances for every account (or directly manipulate `DynamicPropertiesStore` state in a test harness, as done in `CalculateGlobalLimitHardenTest`/`RepositoryImplHardenTest`). [4](#0-3) 
2. Enable hardened resource calculation (`allow-harden-resource-calculation` chain parameter, which controls `VMConfig.allowHardenResourceCalculation()`).
3. Broadcast any `TriggerSmartContractTransaction` from an account holding frozen-for-energy balance ≥ `TRX_PRECISION`, causing `RepositoryImpl.calculateGlobalEnergyLimit` to execute the `BigInteger.divide(BigInteger.valueOf(0))` path and throw `ArithmeticException`, since production JVMs run with assertions disabled and thus never trip the `assert totalEnergyWeight > 0;` guard.

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

**File:** framework/src/test/java/org/tron/core/vm/repository/RepositoryImplHardenTest.java (L1-1)
```java
package org.tron.core.vm.repository;
```
