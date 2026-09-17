Confirmed: `calculateGlobalLimitV1` performs an unguarded `BigInteger.divide(BigInteger.valueOf(totalWeight))` with no zero check of its own, relying entirely on the caller (`EnergyProcessor.calculateGlobalEnergyLimit`) to prevent `totalWeight == 0`. That caller's guard is incomplete (relies on a no-op `assert`), which is the crux of the analog finding below.

### Title
Division-by-zero DoS in `EnergyProcessor.calculateGlobalEnergyLimit` when `totalEnergyWeight` is zero on the legacy (pre-`supportUnfreezeDelay`) resource path - (File: chainbase/src/main/java/org/tron/core/db/EnergyProcessor.java)

### Summary
Analogous to the Alchemix `totalShares == 0` division-by-zero, `EnergyProcessor.calculateGlobalEnergyLimit()` divides an account's energy weight by the network-wide `totalEnergyWeight` dynamic property. The only zero-guard is gated behind the `allowNewReward()` hard-fork flag; when that flag is off, the code falls back to a Java `assert` (a no-op unless the JVM is started with `-ea`), and then unconditionally calls `calculateGlobalLimitV1()`, which performs `BigInteger.valueOf(weight).multiply(...).divide(BigInteger.valueOf(totalWeight))` with no internal zero check.

### Finding Description
`calculateGlobalEnergyLimit()` in [1](#0-0)  reads `totalEnergyWeight` from `DynamicPropertiesStore` and only returns `0` early when `allowNewReward()` is true and the weight is non-positive:
```
if (dynamicPropertiesStore.allowNewReward() && totalEnergyWeight <= 0) {
  return 0;
} else {
  assert totalEnergyWeight > 0;
}
if (hardenCalculation()) {
  return calculateGlobalLimitV1(frozeBalance, totalEnergyLimit, totalEnergyWeight);
}
```
If `allowNewReward()` is false (i.e., the network has not yet activated that proposal) and `totalEnergyWeight` is `0` (e.g., no account has ever frozen TRX for energy under the legacy model, or all such freezes have been undone), the `assert` does nothing in a production JVM (assertions are disabled by default), and execution proceeds to `calculateGlobalLimitV1()` [2](#0-1) , which performs an unguarded `BigInteger` division by `totalWeight`, throwing `ArithmeticException: BigInteger divide by zero`.

This method is invoked from `useEnergy()` [3](#0-2) , which is on the hot path for every smart-contract invocation that consumes energy — i.e., it is reachable from any unprivileged, unauthenticated account simply by broadcasting a `TriggerSmartContract` transaction against any deployed contract, as long as the caller's account has frozen TRX for energy under the legacy (pre-`supportUnfreezeDelay`) freeze model.

### Impact Explanation
An uncaught `ArithmeticException` thrown mid-transaction-execution inside the resource-accounting path is not a normal `ContractValidateException`/`ContractExeException` that gets gracefully converted to a failed-transaction receipt; it is a `RuntimeException` propagating out of core block-application/resource-consumption logic. Depending on how far up the call stack it is caught, this can either cause the individual transaction processing to blow up unexpectedly, or — if uncaught at a higher layer during block application — cause a node to halt/crash while validating a block that exercises this path, which is a liveness/availability impact (matches the "node crash or halt" acceptance criterion).

### Likelihood Explanation
Reachability depends on the specific combination of hard-fork flags: `supportUnfreezeDelay()` must be false (i.e., the "new resource model"/StakeV2 hard fork not yet active) and `allowNewReward()` false, with `totalEnergyWeight == 0` while at least one account still holds legacy `frozenBalance` for energy `>= TRX_PRECISION`. On networks where these proposals have already been permanently activated (which is typically the case on mature/current mainnet-like chains), this specific legacy branch is dead code and not reachable, which significantly limits the current real-world likelihood; it is primarily a residual defect in the legacy path guarding logic that would only be exploitable on non-yet-upgraded networks or fresh testnets/private chains where `allowNewReward` remains at its default value.

### Recommendation
Do not rely on `allowNewReward()` to gate the zero-check. Make the `totalEnergyWeight <= 0` short-circuit unconditional (mirroring the fix already present in `calculateGlobalEnergyLimitV2`, which unconditionally checks `totalEnergyWeight == 0`), and remove reliance on Java `assert` statements for correctness-critical invariants in production code paths.

### Proof of Concept
1. Deploy/operate a node where `supportUnfreezeDelay()` is false and `allowNewReward()` is false (legacy resource model, pre relevant hard forks), and `dynamicPropertiesStore.getTotalEnergyWeight()` is `0` (no account has frozen for energy yet, or the parameter was reset/never incremented).
2. Enable `allowHardenResourceCalculation` (`hardenCalculation()` true) so `calculateGlobalLimitV1` is used.
3. Have any account freeze TRX for energy via the legacy `FreezeBalanceContract` such that `getAllFrozenBalanceForEnergy() >= TRX_PRECISION` for that account, while the global `totalEnergyWeight` counter remains `0` due to the described flag/ordering conditions.
4. Have that account (or any account) broadcast a `TriggerSmartContract` transaction that consumes energy, invoking `EnergyProcessor.useEnergy()` → `calculateGlobalEnergyLimit()` → `calculateGlobalLimitV1()`, causing `BigInteger.divide(BigInteger.valueOf(0))` to throw `ArithmeticException`.

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
