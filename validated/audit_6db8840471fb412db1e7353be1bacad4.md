## Title
Divide-by-zero via disabled `assert` in `RepositoryImpl.calculateGlobalEnergyLimit` when `totalEnergyWeight` is 0 - (File: `actuator/src/main/java/org/tron/core/vm/repository/RepositoryImpl.java`)

### Summary
`RepositoryImpl.calculateGlobalEnergyLimit()` relies on a Java `assert totalEnergyWeight > 0;` statement to guard against a zero divisor before performing a `BigInteger` division (when hardened resource calculation is enabled) or a double division (legacy path). Java `assert` statements are stripped/no-ops unless the JVM is started with `-ea`, which is not the default for a production node. This means the "guard" does not actually execute in a normal deployment, so if `totalEnergyWeight` is ever `0`, the hardened branch throws an uncaught `ArithmeticException: / by zero`. [1](#0-0) 

### Finding Description
`calculateGlobalEnergyLimit` computes an account's usable energy limit from `totalEnergyWeight` and `totalEnergyLimit`, which are dynamic properties updated as stake is frozen/unfrozen network-wide. The code checks `totalEnergyWeight <= 0` only when `allowNewReward()` is active:

```java
if (dynamicPropertiesStore.allowNewReward() && totalEnergyWeight <= 0) {
  return 0;
}
```
(seen in `EnergyProcessor.calculateGlobalEnergyLimit`, `chainbase/src/main/java/org/tron/core/db/EnergyProcessor.java:145-166`)

The `RepositoryImpl` version (used from the TVM native-contract path, e.g. `delegateResourceAction`/`unDelegateResourceAction` and `DelegateResourceActuator`) has no equivalent conditional check at all — it only has the disabled `assert`: [1](#0-0) 

When `hardenResourceCalculation()` (config `allowHardenResourceCalculation`) is enabled, the code performs `BigInteger.valueOf(totalEnergyLimit).divide(BigInteger.valueOf(totalEnergyWeight))`, which throws `ArithmeticException` for `totalEnergyWeight == 0`, since `BigInteger` division does not silently produce `Infinity` like the legacy double-math branch does. If `totalEnergyWeight` reaches `0` (e.g., early chain state, or after all frozen/energy stake has been unfrozen network-wide before this method is invoked), this call throws an unguarded exception.

Callers that don't catch `ArithmeticException` around this call path (e.g., `DelegateResourceActuator.validate()` chain calling into resource processors, or TVM opcode handlers for `delegateResource`/`unDelegateResource`) would propagate the exception up through actuator `validate()`/`execute()`. Depending on where in the call chain this surfaces (actuator execution during block application vs. TVM opcode execution wrapped in try/catch for `Exception`), this is at minimum a reliability/DoS concern for the resource-delegation flow, and is a direct parallel to the reported kernel CVE's root cause: relying on an assumption that a value is never zero without an actual runtime check, where the "sanity check" that exists (a disabled `assert`) provides no real protection.

### Impact Explanation
An unguarded division by a network-wide, dynamically-changing property (`totalEnergyWeight`) that a caller assumes is always positive via an `assert` that does not execute in production is a maintainability/DoS risk: any code path that reaches `totalEnergyWeight == 0` under the hardened-calculation flag will throw at runtime instead of being handled gracefully, exactly analogous to the fbdev `pixclock` divide-by-zero bug where an unchecked value caused a kernel divide-by-zero crash.

### Likelihood Explanation
Likelihood is uncertain: reaching `totalEnergyWeight == 0` requires unusual global-state conditions (e.g., no accounts have any energy stake frozen network-wide), which is unlikely on an established mainnet but plausible on a fresh chain, a low-participation private/test network, or momentarily during an edge-case unfreeze sequence, especially since `allowNewReward()`-gated protection is bypassed for the `RepositoryImpl` variant entirely.

### Recommendation
Replace the `assert totalEnergyWeight > 0;` in `RepositoryImpl.calculateGlobalEnergyLimit` (and audit any sibling bandwidth/energy limit calculations for the same disabled-assert pattern) with an explicit runtime check, e.g. `if (totalEnergyWeight <= 0) { return 0; }`, mirroring the guard already present in `EnergyProcessor`/`BandwidthProcessor`. Ensure this guard is unconditional (not gated behind `allowNewReward()`), and add a regression test exercising `totalEnergyWeight == 0` with `allowHardenResourceCalculation` enabled to confirm no `ArithmeticException` is thrown.

### Proof of Concept
Not able to fully construct a confirmed end-to-end trigger within the scope of this investigation — reaching `totalEnergyWeight == 0` requires specific chain-wide state (no active energy-weight stake) that I could not conclusively confirm is reachable purely from a single unprivileged transaction without deeper analysis of `addTotalEnergyWeight` accounting across all freeze/unfreeze/delegate paths. The core code defect — a disabled `assert` standing in for a real zero-check before `BigInteger` division — is confirmed by direct code inspection: [2](#0-1) , contrasted with the actual runtime guard used elsewhere: [3](#0-2) .

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

**File:** chainbase/src/main/java/org/tron/core/db/EnergyProcessor.java (L156-160)
```java
    if (dynamicPropertiesStore.allowNewReward() && totalEnergyWeight <= 0) {
      return 0;
    } else {
      assert totalEnergyWeight > 0;
    }
```
