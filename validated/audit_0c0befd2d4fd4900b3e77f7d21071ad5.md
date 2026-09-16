### Title
Missing zero-guard on `totalEnergyWeight` in `RepositoryImpl.calculateGlobalEnergyLimit` causes division-by-zero (`ArithmeticException`) during TVM contract execution - ([File: actuator/src/main/java/org/tron/core/vm/repository/RepositoryImpl.java])

### Summary
`RepositoryImpl.calculateGlobalEnergyLimit`, used by the TVM/actuator path to compute an account's global energy limit for contract execution, relies on an `assert totalEnergyWeight > 0` instead of an explicit runtime check before dividing by `totalEnergyWeight`. Java assertions are disabled by default in production JVMs (no `-ea` flag), so if `totalEnergyWeight` ever reaches `0`, execution falls through to a `BigInteger` division (when hardened resource calculation is enabled) that throws `ArithmeticException`, or to a double division that silently produces `Infinity`/`NaN`. This mirrors the reported bug class: a per-share/weight denominator that legitimately can reach zero and is not defensively guarded at the point of division, unlike sibling implementations that do guard it.

### Finding Description
`EnergyProcessor.calculateGlobalEnergyLimit` and `BandwidthProcessor.calculateGlobalNetLimit`/`calculateGlobalNetLimitV2` explicitly check `totalEnergyWeight == 0` / `totalNetWeight == 0` and return `0` early: [1](#0-0) [2](#0-1) 

However, `RepositoryImpl.calculateGlobalEnergyLimit` (used from the TVM/native-contract execution path via `Repository`) only has an `assert totalEnergyWeight > 0` guard, which is compiled out in a normal production JVM run (assertions disabled by default), and then unconditionally proceeds to a `BigInteger` divide when hardened calculation is active: [3](#0-2) 

The `hardenResourceCalculation()`/`calculateGlobalLimitV1` path performs `BigInteger.valueOf(...).divide(BigInteger.valueOf(totalEnergyWeight))`, which throws `ArithmeticException: BigInteger divide by zero` when `totalEnergyWeight == 0`: [4](#0-3) 

This is the same bug class as the report: a total/denominator value (`totalSupply` in the report, `totalEnergyWeight` here) that is expected to be non-zero under normal operation but is not defensively checked at every call site before being used as a divisor, leading to a hard revert of an otherwise legitimate operation.

### Impact Explanation
If `totalEnergyWeight` reaches `0` on-chain (e.g., through a combination of `UnfreezeBalance`/`UnfreezeBalanceV2` operations reducing all network-wide energy-frozen stake to zero, a state theoretically reachable since freezing/unfreezing energy resources is fully permissionless), any subsequent call into `RepositoryImpl.calculateGlobalEnergyLimit` from contract execution (delegate/undelegate resource processing, TVM energy accounting) would throw an uncaught `ArithmeticException` when hardened resource calculation (`allowHardenResourceCalculation`) is enabled. Because this code executes inside transaction/contract processing rather than user-facing validation, an uncaught runtime exception at this layer risks aborting transaction execution unexpectedly or, in the worst case, propagating up through block-application logic, which is categorized in-scope as a node crash/halt condition. This would degrade availability of contract-related transaction processing network-wide until `totalEnergyWeight` recovers, which is a High-severity, protocol-level availability issue rather than a simple per-user revert.

### Likelihood Explanation
Likelihood is Medium: reaching `totalEnergyWeight == 0` network-wide requires essentially every account with frozen-for-energy balance to unfreeze simultaneously, which is unlikely under real economic conditions but is not prevented by any protocol-level floor and is fully reachable through ordinary permissionless `UnfreezeBalance`/`UnfreezeBalanceV2` transactions from unprivileged accounts — no malicious SR/witness/node collusion is required. The condition is also reachable more easily on private/side testnets or in edge conditions during resource migrations, and once triggered, it affects every subsequent hardened-calculation-path energy computation, not just a single user's transaction.

### Recommendation
Add an explicit runtime guard in `RepositoryImpl.calculateGlobalEnergyLimit` (and any other direct call sites that divide by `totalEnergyWeight`/`totalNetWeight` without such guard) mirroring the pattern already used in `EnergyProcessor`/`BandwidthProcessor`:
```java
long totalEnergyWeight = getDynamicPropertiesStore().getTotalEnergyWeight();
if (totalEnergyWeight <= 0) {
  return 0;
}
```
Do not rely on `assert` for correctness-critical invariants, since assertions are disabled by default in production deployments.

### Proof of Concept
Because this analysis is based on static code review of the java-tron repository without running the node, an end-to-end PoC transaction sequence was not executed. Conceptually:
1. Drive `totalEnergyWeight` to `0` via network-wide `UnfreezeBalanceV2` transactions removing all energy-frozen stake (permissionless, reachable by any account with frozen-for-energy balance).
2. Enable/have `allowHardenResourceCalculation` active (already the recommended/hardened path in this codebase, evidenced by dedicated tests in `CalculateGlobalLimitHardenTest.java` and `RepositoryImplHardenTest.java`).
3. Trigger any code path calling `RepositoryImpl.calculateGlobalEnergyLimit` (e.g., delegate/undelegate resource processing or TVM contract energy accounting) — `BigInteger.divide(BigInteger.ZERO)` throws `ArithmeticException`.

This mirrors the referenced test coverage confirming `ArithmeticException` is thrown when a weight/limit divisor is zero under hardened calculation, e.g.: [5](#0-4)

### Citations

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

**File:** chainbase/src/main/java/org/tron/core/db/ResourceProcessor.java (L350-357)
```java
  protected long calculateGlobalLimitV1(long frozeBalance,
      long totalLimit, long totalWeight) {
    long weight = frozeBalance / TRX_PRECISION;
    return BigInteger.valueOf(weight)
        .multiply(BigInteger.valueOf(totalLimit))
        .divide(BigInteger.valueOf(totalWeight))
        .longValueExact();
  }
```

**File:** framework/src/test/java/org/tron/core/db/CalculateGlobalLimitHardenTest.java (L130-141)
```java
  @Test
  public void testGlobalNetLimitOverflowDetectedWithHardening() {
    dbManager.getDynamicPropertiesStore().saveTotalNetLimit(Long.MAX_VALUE / 2);
    dbManager.getDynamicPropertiesStore().saveTotalNetWeight(1L);
    ownerCapsule.setFrozenForBandwidth(Long.MAX_VALUE / 4, 0L);
    dbManager.getAccountStore().put(ownerCapsule.getAddress().toByteArray(), ownerCapsule);

    dbManager.getDynamicPropertiesStore().saveAllowHardenResourceCalculation(1);

    Assert.assertThrows(ArithmeticException.class,
        () -> bandwidthProcessor.calculateGlobalNetLimit(ownerCapsule));
  }
```
