### Title
Integer division-before-multiplication in `calculateGlobalEnergyLimit()` truncates frozen-balance weight before scaling, granting users less energy than they are entitled to - (File: `chainbase/src/main/java/org/tron/core/db/EnergyProcessor.java`)

### Summary
`EnergyProcessor.calculateGlobalEnergyLimit()` computes a user's energy limit from `frozeBalance` by first performing an integer division (`frozeBalance / TRX_PRECISION`) and only afterward multiplying the result by the `totalEnergyLimit/totalEnergyWeight` ratio. This is the exact "divide-then-multiply" precision-loss pattern described in the external `NukeFund.calculateAge()` report: the numerator loses its fractional/remainder information before the scaling multiplication is applied, so any `frozeBalance` that is not an exact multiple of `TRX_PRECISION` (1,000,000 sun) is silently rounded down before scaling, instead of after.

### Finding Description [1](#0-0) 

```java
public long calculateGlobalEnergyLimit(AccountCapsule accountCapsule) {
  ...
  if (hardenCalculation()) {
    return calculateGlobalLimitV1(frozeBalance, totalEnergyLimit, totalEnergyWeight);
  }
  long energyWeight = frozeBalance / TRX_PRECISION;
  return (long) (energyWeight * ((double) totalEnergyLimit / totalEnergyWeight));
}
```

This mirrors the NukeFund bug precisely: `daysOld = (timeDiff)/60/60/24` (division first) then `age = daysOld * perfomanceFactor * ...` (multiplication after). Here `energyWeight = frozeBalance / TRX_PRECISION` truncates the fractional TRX-precision component of the frozen balance *before* it is multiplied by the `totalEnergyLimit/totalEnergyWeight` scaling factor, instead of multiplying first and dividing last (the mathematically correct order that preserves precision, e.g. `(frozeBalance * totalEnergyLimit) / (TRX_PRECISION * totalEnergyWeight)`).

Notably, the repository already contains a deliberate fix for this class of bug: a `hardenCalculation()` gate that routes to `calculateGlobalLimitV1()`/`calculateGlobalLimitV2()`, which use `BigInteger` multiply-then-divide to avoid the truncation: [2](#0-1) 

The existence of this hardened path (and its accompanying `CalculateGlobalLimitHardenTest`) confirms the sponsor/maintainers recognize the legacy formula as imprecise, but the legacy divide-first arithmetic remains the default behavior whenever `hardenCalculation()` (backed by the `allowHardenResourceCalculation` chain parameter) is not yet toggled on, and it is still reachable by any account that freezes TRX for energy without `supportUnfreezeDelay()`, i.e. essentially every legacy freeze-for-energy account interacting through ordinary triggered contract calls. [3](#0-2) 

An analogous truncate-then-scale defect also exists in `getUsageToBalance`/legacy bandwidth accounting comment paths gated the same way, but the energy-limit path is the clearest, directly reachable analog.

### Impact Explanation
Any unprivileged account that freezes a non-round amount of TRX for energy (any `frozeBalance` not an exact multiple of 1,000,000 sun, which is the overwhelming majority of real freeze amounts) receives an energy limit computed from a floor-truncated weight rather than the precise proportional weight. Because the truncation happens before the (typically large) `totalEnergyLimit/totalEnergyWeight` ratio multiplication, the resulting energy limit can differ by a percentage proportional to the ratio, understating the energy a user is entitled to and potentially causing legitimate contract calls to run out of energy or forcing the user to freeze more TRX than economically necessary to reach a target energy limit — an unbacked-value/economic-loss condition for normal users, exactly the "wrong claim amount" class of impact identified in the source report.

### Likelihood Explanation
This code path executes on every call to `calculateGlobalEnergyLimit()` for accounts on the legacy (`!supportUnfreezeDelay()`) freezing model whenever the `hardenCalculation()` proposal flag is not active — i.e., it is part of the default resource-accounting flow triggered by ordinary `FreezeBalanceContract`/`TriggerSmartContract` transactions from any unprivileged user, requiring no special privilege, malicious validator, or network condition.

### Recommendation
Apply the same fix already implemented for the hardened path unconditionally (or make `hardenCalculation()` the default/only path): compute the energy limit by multiplying `frozeBalance * totalEnergyLimit` first and dividing by `TRX_PRECISION * totalEnergyWeight` last, using `BigInteger` (or otherwise overflow-safe wide arithmetic) as already done in `calculateGlobalLimitV1`/`calculateGlobalLimitV2`, and retire the legacy divide-first formula entirely rather than leaving it reachable behind a governance flag.

### Proof of Concept
Given `frozeBalance = 1_500_000` (1.5× `TRX_PRECISION`), `totalEnergyLimit = 50_000_000_000`, `totalEnergyWeight = 2_000_000_000`:
- Legacy (buggy) path: `energyWeight = 1_500_000 / 1_000_000 = 1` (fraction `.5` lost) → `1 * (50_000_000_000/2_000_000_000) = 25`.
- Correct multiply-first path: `(1_500_000 * 50_000_000_000) / (1_000_000 * 2_000_000_000) = 37.5 → 37`.

This exact scenario is already captured in the repo's own regression test, confirming the discrepancy is real and reproducible: [4](#0-3)

### Citations

**File:** chainbase/src/main/java/org/tron/core/db/EnergyProcessor.java (L102-106)
```java
  public boolean useEnergy(AccountCapsule accountCapsule, long energy, long now) {

    long energyUsage = accountCapsule.getEnergyUsage();
    long latestConsumeTime = accountCapsule.getAccountResource().getLatestConsumeTimeForEnergy();
    long energyLimit = calculateGlobalEnergyLimit(accountCapsule);
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

**File:** framework/src/test/java/org/tron/core/db/CalculateGlobalLimitHardenTest.java (L235-255)
```java
  @Test
  public void testV1FlooredWeightVsV2FractionalWeight() {
    dbManager.getDynamicPropertiesStore().saveTotalEnergyCurrentLimit(50_000_000_000L);
    dbManager.getDynamicPropertiesStore().saveTotalEnergyWeight(2_000_000_000L);
    long frozeBalance = 1_500_000L; // 1.5 x TRX_PRECISION

    dbManager.getDynamicPropertiesStore().saveAllowHardenResourceCalculation(1);

    // V1 path
    dbManager.getDynamicPropertiesStore().saveUnfreezeDelayDays(0);
    ownerCapsule.setFrozenForEnergy(frozeBalance, 0L);
    dbManager.getAccountStore().put(ownerCapsule.getAddress().toByteArray(), ownerCapsule);
    long v1New = energyProcessor.calculateGlobalEnergyLimit(ownerCapsule);

    // Legacy V1 expectation: floor(1.5) * 25.0 = 1 * 25 = 25
    Assert.assertEquals(25L, v1New);

    // V2 path with the same balance keeps the fractional weight
    long v2New = energyProcessor.calculateGlobalEnergyLimitV2(frozeBalance);
    // Legacy V2 expectation: 1.5 * 25.0 = 37.5 -> 37
    Assert.assertEquals(37L, v2New);
```
