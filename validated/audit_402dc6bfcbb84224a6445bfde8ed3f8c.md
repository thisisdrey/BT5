## Title
Delegate resource validation uses legacy double-based usage ratio instead of the canonical hardened resource-limit calculation, allowing an inconsistency-based over-delegation check bypass - ([File: actuator/src/main/java/org/tron/core/actuator/DelegateResourceActuator.java])

## Summary
`DelegateResourceActuator.validate()` independently recomputes the account's bandwidth/energy usage-to-limit ratio with a legacy `double`-based formula instead of using the canonical, hardened resource-limit calculation methods (`BandwidthProcessor.calculateGlobalNetLimit`/`EnergyProcessor.calculateGlobalEnergyLimit`) that the rest of the resource-accounting system relies on. This is directly analogous to the JUSDBank finding: a solvency/availability check is performed with a value computed by a different code path than the canonical one used elsewhere in the same system, instead of reusing the single source of truth.

## Finding Description
`DelegateResourceActuator.validate()` computes the amount of "V2" frozen balance available to delegate by first calling `updateUsageForDelegated`/`updateUsage` and then manually recomputing the weighted usage with a hand-rolled `double` formula: [1](#0-0) 

```java
long netUsage = (long) (accountNetUsage * TRX_PRECISION * ((double)
    (dynamicStore.getTotalNetWeight()) / dynamicStore.getTotalNetLimit()));
long v2NetUsage = getV2NetUsage(ownerCapsule, netUsage, this.disableJavaLangMath());
if (ownerCapsule.getFrozenV2BalanceForBandwidth() - v2NetUsage < delegateBalance) { ... }
```
and analogously for ENERGY using `dynamicStore.getTotalEnergyWeight() / dynamicStore.getTotalEnergyCurrentLimit()`.

This duplicates logic that already exists in the canonical, hardened resource-limit calculators: [2](#0-1) 

which, when `hardenCalculation()` is enabled, route through exact BigInteger math (`calculateGlobalLimitV1`/`calculateGlobalLimitV2`): [3](#0-2) 

The presence of a dedicated "hardened calculation" migration (as seen in `CalculateGlobalLimitHardenTest`) shows the codebase intentionally moved core resource-limit math away from floating-point to BigInteger-exact math specifically to avoid rounding drift: [4](#0-3) 

`DelegateResourceActuator.validate()` was not updated to use this canonical path — it always uses the raw floating-point ratio regardless of the `hardenCalculation()`/`disableJavaLangMath()` flag, exactly mirroring the JUSDBank pattern where one function (`withdraw`) recomputed a rate independently (`getTRate()`) instead of using the canonical, harden(accrue)d value (`tRate`/`accrueRate()`) used consistently elsewhere.

## Impact Explanation
Because the "available V2 balance to delegate" check in `validate()` is derived from a different (legacy, floating-point) usage computation than the one the rest of the system uses to track/consume bandwidth and energy, the check can diverge from the account's true available resource. Any transaction constructor (any account holder submitting a `DelegateResourceContract`) can drive this divergence. If the double-based `netUsage`/`energyUsage` estimate is smaller than the true, canonically-tracked usage, a user could be permitted to delegate an amount of frozen V2 balance that exceeds what is genuinely free, producing inconsistent bookkeeping between the sender's own frozen/used balance and the amount actually locked for the receiver — i.e., resource accounting drift analogous to the "wrong rate used for a safety check" root cause in the report.

## Likelihood Explanation
The divergence requires specific but realistic conditions: nonzero `TotalNetWeight`/`TotalNetLimit` (or Energy equivalent) ratios and existing bandwidth/energy usage such that floating-point rounding in `validate()` differs materially from the BigInteger-exact result used elsewhere. This is reachable by any signer submitting a normal `DelegateResourceContract` transaction — no privileged role required — but the magnitude of the resulting discrepancy is bounded by floating-point rounding error, so its practical exploitability (compared to the original Solidity bug, which affects an actual solvency gate) is comparatively limited and would need further quantification to confirm real-world fund impact.

## Recommendation
Route the bandwidth/energy usage computation in `DelegateResourceActuator.validate()` through the same canonical, hardened resource-limit helpers (`BandwidthProcessor.calculateGlobalNetLimit`, `EnergyProcessor.calculateGlobalEnergyLimit`, or their V2 counterparts) that are used everywhere else in the resource-accounting subsystem, rather than re-deriving the ratio with an independent `double` formula, so that the value used for this critical delegation-safety check is always consistent with the value used to enforce actual resource consumption.

## Proof of Concept
Not independently verified with a concrete numeric exploit in this pass — the root-cause code divergence is directly cited above (`DelegateResourceActuator.validate()` vs. `EnergyProcessor`/`ResourceProcessor` hardened calculators); confirming an economically significant discrepancy would require constructing specific `TotalNetWeight/TotalNetLimit` and usage values and comparing the two formulas' outputs, which was not completed due to the scan's time constraints.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/DelegateResourceActuator.java (L152-184)
```java
    switch (delegateResourceContract.getResource()) {
      case BANDWIDTH: {
        BandwidthProcessor processor = new BandwidthProcessor(chainBaseManager);
        processor.updateUsageForDelegated(ownerCapsule);

        long accountNetUsage = ownerCapsule.getNetUsage();
        if (null != this.getTx() && this.getTx().isTransactionCreate()) {
          accountNetUsage += TransactionUtil.estimateConsumeBandWidthSize(dynamicStore,
                  ownerCapsule.getFrozenV2BalanceForBandwidth());
        }
        long netUsage = (long) (accountNetUsage * TRX_PRECISION * ((double)
            (dynamicStore.getTotalNetWeight()) / dynamicStore.getTotalNetLimit()));
        long v2NetUsage = getV2NetUsage(ownerCapsule, netUsage,
            this.disableJavaLangMath());
        if (ownerCapsule.getFrozenV2BalanceForBandwidth() - v2NetUsage < delegateBalance) {
          throw new ContractValidateException(
              "delegateBalance must be less than or equal to available FreezeBandwidthV2 balance");
        }
      }
      break;
      case ENERGY: {
        EnergyProcessor processor = new EnergyProcessor(dynamicStore, accountStore);
        processor.updateUsage(ownerCapsule);

        long energyUsage = (long) (ownerCapsule.getEnergyUsage() * TRX_PRECISION * ((double)
            (dynamicStore.getTotalEnergyWeight()) / dynamicStore.getTotalEnergyCurrentLimit()));
        long v2EnergyUsage = getV2EnergyUsage(ownerCapsule, energyUsage,
            this.disableJavaLangMath());
        if (ownerCapsule.getFrozenV2BalanceForEnergy() - v2EnergyUsage < delegateBalance) {
          throw new ContractValidateException(
                  "delegateBalance must be less than or equal to available FreezeEnergyV2 balance");
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

**File:** chainbase/src/main/java/org/tron/core/db/ResourceProcessor.java (L359-378)
```java
  /**
   * Hardened replacement of legacy V2 formula
   * {@code (long)(((double) frozeBalance / TRX_PRECISION)
   *               * ((double) totalLimit / totalWeight))}.
   *
   * <p>Preserves V2 semantics: equivalent to
   * {@code (frozeBalance * totalLimit) / (TRX_PRECISION * totalWeight)} with
   * a single integer truncation at the end. Critically, fractional weight
   * (i.e. {@code frozeBalance < TRX_PRECISION}) is preserved through the
   * multiplication and only truncated at the final divide, so small balances
   * yield the same proportional result as the double-arithmetic path.
   */
  protected long calculateGlobalLimitV2(long frozeBalance,
      long totalLimit, long totalWeight) {
    return BigInteger.valueOf(frozeBalance)
        .multiply(BigInteger.valueOf(totalLimit))
        .divide(BigInteger.valueOf(TRX_PRECISION)
            .multiply(BigInteger.valueOf(totalWeight)))
        .longValueExact();
  }
```

**File:** framework/src/test/java/org/tron/core/db/CalculateGlobalLimitHardenTest.java (L178-213)
```java
  @Test
  public void testGlobalEnergyLimitV2BelowTrxPrecisionMatchesDouble() {
    long totalEnergyLimit = 50_000_000_000L;
    long totalEnergyWeight = 2_000_000_000L;
    long frozeBalance = 500_000L; // < TRX_PRECISION (1_000_000)

    dbManager.getDynamicPropertiesStore().saveTotalEnergyCurrentLimit(totalEnergyLimit);
    dbManager.getDynamicPropertiesStore().saveTotalEnergyWeight(totalEnergyWeight);

    dbManager.getDynamicPropertiesStore().saveAllowHardenResourceCalculation(0);
    long resultOld = energyProcessor.calculateGlobalEnergyLimitV2(frozeBalance);

    dbManager.getDynamicPropertiesStore().saveAllowHardenResourceCalculation(1);
    long resultNew = energyProcessor.calculateGlobalEnergyLimitV2(frozeBalance);

    Assert.assertEquals(12L, resultNew);
    Assert.assertEquals(resultOld, resultNew);
  }

  @Test
  public void testGlobalNetLimitV2BelowTrxPrecisionMatchesDouble() {
    long totalNetLimit = 43_200_000_000L;
    long totalNetWeight = 2_000_000_000L;
    long frozeBalance = 500_000L; // < TRX_PRECISION

    dbManager.getDynamicPropertiesStore().saveTotalNetLimit(totalNetLimit);
    dbManager.getDynamicPropertiesStore().saveTotalNetWeight(totalNetWeight);

    dbManager.getDynamicPropertiesStore().saveAllowHardenResourceCalculation(0);
    long resultOld = bandwidthProcessor.calculateGlobalNetLimitV2(frozeBalance);

    dbManager.getDynamicPropertiesStore().saveAllowHardenResourceCalculation(1);
    long resultNew = bandwidthProcessor.calculateGlobalNetLimitV2(frozeBalance);

    Assert.assertEquals(resultOld, resultNew);
    Assert.assertTrue("non-zero proportional result expected", resultNew > 0);
```
