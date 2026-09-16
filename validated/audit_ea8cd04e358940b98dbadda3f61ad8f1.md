### Title
Unchecked `assert` guard allows `ArithmeticException` divide-by-zero crash in energy limit calculation - ([File: actuator/src/main/java/org/tron/core/vm/repository/RepositoryImpl.java])

### Summary
`RepositoryImpl.calculateGlobalEnergyLimit` relies on a Java `assert` statement instead of a real conditional check to guard against `totalEnergyWeight == 0` before dividing by it. Java assertions are disabled by default in production JVMs (no `-ea` flag), so this "check" is compiled out and provides no protection at runtime — directly analogous to CVE-2024-25739, where `create_empty_lvol` was missing a real check for a zero-valued size before it was used as a divisor/allocation size, causing a crash.

### Finding Description [1](#0-0) 

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

Compare this to the sibling implementation in `EnergyProcessor.calculateGlobalEnergyLimit`, which contains an actual guarding `if` statement in addition to the (dead) `assert`: [2](#0-1) 

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
```

`RepositoryImpl.calculateGlobalEnergyLimit` is missing the equivalent real `if (... totalEnergyWeight <= 0) return 0;` short-circuit that its sibling class has. Because `assert` statements are stripped/no-ops at runtime by default (Java assertions require the `-ea` JVM flag, which is not the default deployment configuration for java-tron nodes), the only actual protection against `totalEnergyWeight == 0` in `RepositoryImpl` is missing.

If `hardenResourceCalculation()` (`VMConfig.allowHardenResourceCalculation()`) is enabled and `totalEnergyWeight` is `0`, the code executes `BigInteger.valueOf(totalEnergyLimit).divide(BigInteger.valueOf(0))`, which throws an uncaught `ArithmeticException: BigInteger divide by zero`.

`totalEnergyWeight` is a global chain-wide dynamic property that tracks total energy weight across all frozen accounts; it can legitimately become `0` (e.g., early chain bootstrap before anyone freezes for energy, or transiently if freezing/unfreezing operations reduce it to zero via `UnfreezeBalanceV2Actuator`/`UnDelegateResourceActuator`/`UnfreezeBalanceV2Processor`, which call `addTotalEnergyWeight` with negative deltas). `RepositoryImpl` is the repository implementation used throughout TVM contract execution (energy-related resource lookups reachable during any contract call that queries account energy limits), so this path is reachable by any user triggering ordinary contract execution when the chain state has `totalEnergyWeight == 0`.

### Impact Explanation
An uncaught `ArithmeticException` during transaction/contract execution in `RepositoryImpl` (part of the TVM execution path used by every contract call) can propagate up through the actuator/VM execution stack. If not caught by a broader `try/catch` at a higher layer, this can abort block application or transaction processing unexpectedly, resulting in a node crash / denial of service for the node processing the block — matching the CVE's "attempt to allocate/act on a zero value derived from a missing check, and crash" pattern.

### Likelihood Explanation
This requires `totalEnergyWeight == 0` (a chain-state precondition, not attacker-controlled directly) combined with `allowHardenResourceCalculation()` being enabled. `totalEnergyWeight` reaching zero is plausible on low-activity/private/testing java-tron networks or transiently during resource unfreeze operations, and `hardenResourceCalculation` is a feature flag that networks may enable. Given this combination is state-dependent rather than trivially attacker-triggerable on mainnet, likelihood is moderate.

### Recommendation
Add an explicit non-`assert` guard in `RepositoryImpl.calculateGlobalEnergyLimit` (and its net/bandwidth-limit analog `calculateGlobalNetLimit`, if it has a similar gap) mirroring `EnergyProcessor`'s real `if (totalEnergyWeight <= 0) return 0;` check before any division, so that the safety check executes unconditionally regardless of JVM assertion settings.

### Proof of Concept
1. On a java-tron network, drive `totalEnergyWeight` (in `DynamicPropertiesStore`) to `0`, e.g., by having all previously energy-frozen accounts fully `UnfreezeBalanceV2`/`UnDelegateResource` so the aggregate weight is exhausted.
2. Ensure `allowHardenResourceCalculation` is enabled (chain parameter).
3. Trigger any smart-contract execution path that calls `RepositoryImpl.calculateGlobalEnergyLimit` (any account energy-limit query reached during ordinary contract call/energy accounting).
4. `BigInteger.valueOf(totalEnergyLimit).divide(BigInteger.valueOf(0))` throws `ArithmeticException`, uncaught by this method, propagating into the caller.

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
