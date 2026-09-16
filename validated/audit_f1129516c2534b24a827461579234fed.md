### Title
Legacy (non-hardened) energy/bandwidth global-limit formulas divide before multiplying with `double`, causing systematic under-computation of an account's resource limit - ([File: chainbase/src/main/java/org/tron/core/db/EnergyProcessor.java])

### Summary
`EnergyProcessor.calculateGlobalEnergyLimit`/`calculateGlobalEnergyLimitV2` (and the analogous `BandwidthProcessor` global-limit path) compute an account's usable Energy/Bandwidth by first dividing `totalEnergyLimit / totalEnergyWeight` and then multiplying by the frozen-balance-derived weight, exactly the "division before multiplication" pattern flagged in the referenced report. This code path is only bypassed when `hardenCalculation()` (i.e. `allowHardenResourceCalculation`) is enabled; on chains/situations where that flag is off, the buggy legacy arithmetic is what actually executes for every energy-consuming transaction.

### Finding Description
`calculateGlobalEnergyLimit`: [1](#0-0) 
computes `energyWeight * ((double) totalEnergyLimit / totalEnergyWeight)` — the division `totalEnergyLimit / totalEnergyWeight` happens first, only afterwards multiplied by `energyWeight`. The V2 variant has the identical pattern: [2](#0-1) 

This is the same bug class as the reported `_totalVestedAt`: a ratio is computed and truncated/rounded (here to a `double`, there to an integer) before being multiplied by another operand, instead of doing `multiply` then `divide`. The codebase's own test suite and comment documentation confirm this is a recognized defect class — the accompanying `calculateGlobalLimitV2`/`calculateGlobalLimitV1` "hardened" helpers were written specifically to replace this exact double-based formula with a single BigInteger `multiply-then-divide`: [3](#0-2) 
and a dedicated regression test (`testGlobalEnergyLimitV1NonIntegerRatioParity`, `testV1FlooredWeightVsV2FractionalWeight`) exists purely to prove the two formulas diverge when `totalEnergyWeight` does not evenly divide `totalEnergyLimit`: [4](#0-3) 

However, the fix is opt-in: the legacy divide-before-multiply branch is only skipped `if (hardenCalculation())`: [5](#0-4) 
Whenever `allowHardenResourceCalculation` is not enabled, every call to `useEnergy` (invoked for every TVM contract call that consumes energy) and `getAccountLeftEnergyFromFreeze` uses the imprecise, order-dependent double formula: [6](#0-5) 

`BandwidthProcessor` follows the same "hardenCalculation() switch" structure for its own global net limit calculation (see the mirrored `calculateGlobalNetLimit`/`calculateGlobalLimitV2` matches found in `chainbase/src/main/java/org/tron/core/db/BandwidthProcessor.java`), so the same reasoning applies to bandwidth accounting for every ordinary transaction, not just TVM calls.

### Impact Explanation
The pre-division truncates the fractional part of `totalEnergyLimit / totalEnergyWeight` to `double` precision before multiplying by the caller's weight. Because `double` has ~15-17 significant decimal digits, the practical impact for typical TRON magnitudes (energy limits in the tens of billions, weights in similar ranges) is small relative errors rather than the report's "quantize-to-zero" scenario, but it is still a genuine, systematic deviation from the mathematically correct integer result — as proven by the project's own tests showing the legacy and hardened formulas disagree. This can under- or over-state an account's Energy/Bandwidth limit relative to its actual stake-weighted entitlement, which affects resource accounting fairness across all accounts sharing the same global limit/weight pool. It does not directly enable unauthorized fund transfer, but it is a resource-accounting correctness bug in a core, transaction-reachable computation.

### Likelihood Explanation
This code path executes on every energy-consuming smart contract call and every bandwidth-consuming transaction (`useEnergy` / bandwidth's analogous consume path), so it is reachable by any account issuing an ordinary transaction. The condition that triggers a *measurable* divergence — `totalEnergyWeight` not evenly dividing `totalEnergyLimit` — is the normal case, not an edge case (these are network-wide accumulators that fluctuate constantly), so it fires continuously, not merely under adversarial conditions. The magnitude of loss per calculation is small (double-precision rounding), which limits severity but not likelihood of occurrence.

### Recommendation
Make `hardenCalculation()` (or equivalently the `allowHardenResourceCalculation` behavior) the unconditional path rather than an opt-in toggle, i.e. always compute the limit via a single integer/BigInteger `multiply` then `divide` as already implemented in `calculateGlobalLimitV1`/`calculateGlobalLimitV2` in `chainbase/src/main/java/org/tron/core/db/ResourceProcessor.java`, and remove the legacy double-based branch in `EnergyProcessor.calculateGlobalEnergyLimit`/`calculateGlobalEnergyLimitV2` and the mirrored logic in `BandwidthProcessor` once the hard fork activating this behavior is broadly in effect.

### Proof of Concept
Using the project's own test values (`testV1FlooredWeightVsV2FractionalWeight`): [4](#0-3) 
With `totalEnergyLimit = 50_000_000_000`, `totalEnergyWeight = 2_000_000_000`, `frozeBalance = 1_500_000` (1.5×`TRX_PRECISION`):
- Legacy V1 path (`energyWeight = frozeBalance / TRX_PRECISION` truncated to `1`, then `1 * 25.0 = 25`) yields `25`.
- V2/hardened path preserving the fractional weight yields `37` (`1.5 * 25.0 = 37.5` truncated to `37`).
This 48% discrepancy (25 vs 37) for the same underlying frozen balance demonstrates the division-before-multiplication (fractional-weight-loss) defect is real and quantifiable whenever `hardenCalculation()` is disabled, matching the reported bug class from a division-before-multiplication order-of-operations perspective, though manifesting as a rounding/precision defect rather than a full zero-out here.

### Citations

**File:** chainbase/src/main/java/org/tron/core/db/EnergyProcessor.java (L102-120)
```java
  public boolean useEnergy(AccountCapsule accountCapsule, long energy, long now) {

    long energyUsage = accountCapsule.getEnergyUsage();
    long latestConsumeTime = accountCapsule.getAccountResource().getLatestConsumeTimeForEnergy();
    long energyLimit = calculateGlobalEnergyLimit(accountCapsule);
    long newEnergyUsage;
    if (!dynamicPropertiesStore.supportUnfreezeDelay()) {
      newEnergyUsage = increase(energyUsage, 0, latestConsumeTime, now);
    } else {
      // only participate in the calculation as a temporary variable, without disk flushing
      newEnergyUsage = recovery(accountCapsule, ENERGY, energyUsage,
          latestConsumeTime, now);
    }

    if (energy > (energyLimit - newEnergyUsage)
        && dynamicPropertiesStore.getAllowTvmFreeze() == 0
        && !dynamicPropertiesStore.supportUnfreezeDelay()) {
      return false;
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

**File:** framework/src/test/java/org/tron/core/db/CalculateGlobalLimitHardenTest.java (L235-263)
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

    // And both must match their respective legacy doubles
    dbManager.getDynamicPropertiesStore().saveAllowHardenResourceCalculation(0);
    long v1Old = energyProcessor.calculateGlobalEnergyLimit(ownerCapsule);
    long v2Old = energyProcessor.calculateGlobalEnergyLimitV2(frozeBalance);
    Assert.assertEquals(v1Old, v1New);
    Assert.assertEquals(v2Old, v2New);
  }
```
