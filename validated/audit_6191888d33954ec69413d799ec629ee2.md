### Title
Division by zero / stale-assertion guard in `EnergyProcessor.calculateGlobalEnergyLimit` can throw and block energy accounting on the legacy path - (File: chainbase/src/main/java/org/tron/core/db/EnergyProcessor.java)

### Summary
`EnergyProcessor.calculateGlobalEnergyLimit` divides by `totalEnergyWeight` (the network-wide energy-frozen weight) but only actually validates that value against zero when `dynamicPropertiesStore.allowNewReward()` is true. On the legacy branch it falls back to a bare `assert totalEnergyWeight > 0;` statement, which is compiled out and never executes at runtime unless the JVM is started with `-ea` (assertions are disabled by default in production). This mirrors the reported Ion-pool defect: a numeric precondition is checked in one code path but only "asserted" (i.e., not actually enforced) in another, so a legitimate zero value for the divisor can reach the division operation.

### Finding Description
`calculateGlobalEnergyLimit` is invoked from `EnergyProcessor.useEnergy` [1](#0-0)  and from `getAccountLeftEnergyFromFreeze` [2](#0-1) , both of which sit on the hot path for every energy-consuming transaction (smart-contract calls) and for TVM's `getAccountLeftEnergyFromFreeze` used when computing available energy for a caller.

The vulnerable logic:
```java
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
``` [3](#0-2) 

- If `allowNewReward()` is `false` (the pre-hardfork/legacy path is still selected on chains that have not activated the corresponding proposal), the only guard against `totalEnergyWeight == 0` is `assert`, which does nothing in a standard production JVM.
- If `hardenCalculation()` is enabled, `calculateGlobalLimitV1` performs `BigInteger.valueOf(weight).multiply(...).divide(BigInteger.valueOf(totalWeight))` [4](#0-3) , and dividing a `BigInteger` by `BigInteger.ZERO` throws `ArithmeticException`.
- If `hardenCalculation()` is disabled, the legacy double-arithmetic path divides by `totalEnergyWeight` as a `double`, which does not throw but silently produces `Infinity`/`NaN`, corrupting the computed energy limit for every account queried while the value is zero.

By contrast, the V2/unfreeze-delay path (`calculateGlobalEnergyLimitV2`) contains an explicit, always-enforced check:
```java
if (totalEnergyWeight == 0) {
  return 0;
}
``` [5](#0-4) 
confirming the omission in the legacy branch is inconsistent with the intended invariant enforced elsewhere in the same class, exactly matching the report's bug class: a numeric precondition guarded in one spot but missing (or merely asserted) elsewhere in the same interest/weight-rate computation.

`totalEnergyWeight` is a mutable, chain-wide value adjusted whenever accounts freeze/unfreeze balance for energy (`FreezeBalanceActuator`, `UnfreezeBalanceActuator`, `FreezeBalanceV2Actuator`, `UnfreezeBalanceV2Actuator`, and their TVM native-contract equivalents), so it is reachable and mutable purely through unprivileged, broadcastable freeze/unfreeze transactions.

### Impact Explanation
If `totalEnergyWeight` reaches zero while the legacy (`allowNewReward() == false`) code path and `hardenCalculation()` are both active, every call to `calculateGlobalEnergyLimit` — invoked for essentially all TVM contract execution that consumes energy — throws an uncaught `ArithmeticException`, which propagates out of `useEnergy`/`getAccountLeftEnergyFromFreeze` and can abort the resource-accounting stage for transactions, effectively producing a network-wide denial of service similar to the reported Ion pool `accrueInterest` DoS. Even without hardening, the double-division fallback silently returns corrupted (`Infinity`/`NaN`→`0` via cast) energy limits, letting energy accounting misbehave for every account until `totalEnergyWeight` becomes non-zero again.

### Likelihood Explanation
Likelihood is Low-to-Medium: `allowNewReward()` guards the modern, hardfork-activated behavior, so on networks where that proposal is enabled the zero check is enforced. The exposure only exists on chains/paths still running the legacy branch (`allowNewReward() == false`), similar to how the original Ion report required a narrow, low-liquidity condition (dust supply/borrow) to trigger the flaw. Reaching `totalEnergyWeight == 0` also requires essentially all network-wide energy freezes to be withdrawn, which is unlikely on a mature mainnet but plausible on smaller networks, private chains, or shortly after specific hardfork activation ordering, exactly analogous to a "newly deployed pool with low supply" scenario in the source report.

### Recommendation
Replace the `assert totalEnergyWeight > 0;` statement in `calculateGlobalEnergyLimit` with an unconditional runtime check that returns 0 (or otherwise short-circuits) whenever `totalEnergyWeight <= 0`, regardless of `allowNewReward()`, mirroring the guard already present in `calculateGlobalEnergyLimitV2`. Apply the analogous fix in `BandwidthProcessor.calculateGlobalNetLimit`/`RepositoryImpl.calculateGlobalEnergyLimit`, which have similar branch structures.

### Proof of Concept
Not independently executable without live-chain state; the code path can be reasoned through statically:
1. Ensure `allowNewReward()` is `false` (legacy behavior not yet activated on the network) and `allowHardenResourceCalculation()` is `true`.
2. Have all accounts with frozen-for-energy balance broadcast `UnfreezeBalanceContract`/`UnfreezeBalanceV2Contract` transactions until `dynamicPropertiesStore.getTotalEnergyWeight()` becomes `0` [6](#0-5) .
3. Any subsequent `TriggerSmartContract` transaction from any account with a nonzero `frozeBalance` for energy invokes `EnergyProcessor.useEnergy` → `calculateGlobalEnergyLimit` → `calculateGlobalLimitV1`, which performs `BigInteger` division by `totalEnergyWeight == 0` and throws `ArithmeticException` [4](#0-3) , disrupting energy resource accounting for that transaction and, if unhandled up the call chain, more broadly.

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

**File:** chainbase/src/main/java/org/tron/core/db/EnergyProcessor.java (L182-191)
```java
  public long getAccountLeftEnergyFromFreeze(AccountCapsule accountCapsule) {
    long now = getHeadSlot();
    long energyUsage = accountCapsule.getEnergyUsage();
    long latestConsumeTime = accountCapsule.getAccountResource().getLatestConsumeTimeForEnergy();
    long energyLimit = calculateGlobalEnergyLimit(accountCapsule);

    long newEnergyUsage = recovery(accountCapsule, ENERGY, energyUsage, latestConsumeTime, now);

    return max(energyLimit - newEnergyUsage, 0, this.disableJavaLangMath()); // us
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

**File:** actuator/src/main/java/org/tron/core/actuator/UnfreezeBalanceActuator.java (L1-1)
```java
package org.tron.core.actuator;
```
