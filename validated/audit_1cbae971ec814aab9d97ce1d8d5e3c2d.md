### Title
Precision Loss / Overstatement of Transferable Resource Usage in `UnDelegateResourceActuator` and `UnDelegateResourceProcessor` Due to Floating-Point Division-Before-Multiplication - (File: `actuator/src/main/java/org/tron/core/actuator/UnDelegateResourceActuator.java`, `actuator/src/main/java/org/tron/core/vm/nativecontract/UnDelegateResourceProcessor.java`)

### Summary
`unDelegateMaxUsage` (bandwidth/energy usage cap transferred back from receiver to owner during `UnDelegateResourceContract` / native `unDelegateResource` execution) is computed with `double` arithmetic that divides `unDelegateBalance` by `TRX_PRECISION` before multiplying by `totalNetLimit`/`totalEnergyCurrentLimit` and dividing again by the total weight, instead of doing all multiplications first and a single final division. This mirrors the reported Unitas `_getReserveStatus` bug class: dividing before multiplying causes avoidable rounding/precision loss, here compounded by `double` floating point rather than pure integer math.

### Finding Description
In `UnDelegateResourceActuator.execute` (bandwidth branch): [1](#0-0) 
and the energy branch: [2](#0-1) 

The same pattern is duplicated in the native-contract equivalent used by TVM-triggered `unDelegateResource`: [3](#0-2) [4](#0-3) 

The formula computes `(double) unDelegateBalance / TRX_PRECISION * (totalLimit / totalWeight)` — i.e. division happens both before and interleaved with the multiplication, and the whole computation is done in floating point rather than as `(unDelegateBalance * totalLimit) / (TRX_PRECISION * totalWeight)` using integer/BigInteger arithmetic with a single final truncation. This is exactly the anti-pattern flagged in the external report: intermediate integer/floating-point division before the final scaling multiplication introduces avoidable precision loss, and can even magnify error since `double` has only 53 bits of mantissa precision, unlike the report's simpler integer rounding-down case.

Notably, elsewhere in this same codebase (`RepositoryImpl.usageToBalance`, `EnergyProcessor.calculateGlobalEnergyLimitV2`, `ResourceProcessor.calculateGlobalLimitV2`) the exact same class of bug was identified and hardened by rewriting the formula to multiply everything first in `BigInteger` and perform a single final division, gated behind an `allowHardenResourceCalculation`/`hardenCalculation()` flag: [5](#0-4) [6](#0-5) 

However, `UnDelegateResourceActuator` and `UnDelegateResourceProcessor` were not updated to use this hardened path — they still use the unhardened `double`-based, divide-before-multiply formula unconditionally, with no `hardenCalculation()`/`disableJavaLangMath()` guard around the core division order (only `min()` is guarded).

### Impact Explanation
`unDelegateMaxUsage` caps how much bandwidth/energy usage is transferred from the receiver's account back proportionally when a delegator calls `unDelegateResource` (broadcastable by any account owner via `UnDelegateResourceContract`, or triggered from within a smart contract via the native precompile). Because of the floating-point division-before-multiplication, the computed cap can be lower (or in edge cases higher, due to floating rounding either direction) than the mathematically exact `(unDelegateBalance * totalLimit) / (TRX_PRECISION * totalWeight)` value. Since `transferUsage = min(unDelegateMaxUsage, transferUsage)`, an understated `unDelegateMaxUsage` incorrectly reduces the usage returned to the delegator/owner and left with the receiver, subtly corrupting each account's `netUsage`/`energyUsage` bookkeeping over time. Given resource accounting underpins users' effective free bandwidth/energy allowance (an economic resource with real value, tradable/delegatable), systematic mis-accounting is a state-integrity/fund-equivalent-value issue, consistent with Medium severity as in the original report.

### Likelihood Explanation
This code path executes on every `UnDelegateResourceContract` transaction and every native `unDelegateResource` call from a TVM contract — a very common, unprivileged, single-signed-transaction operation (delegation/staking related). No special permissions or malicious actor role is required; any account that has delegated bandwidth/energy resources and calls undelegate will trigger this arithmetic. The precision defect is deterministic given specific balance/weight/limit ratios (particularly for accounts with large frozen balances or particular weight ratios where floating-point rounding diverges from exact integer math), and the codebase's own hardening effort/tests for the sibling functions (`RepositoryImplHardenTest`, `CalculateGlobalLimitHardenTest`) demonstrate that these divergences are real and measurable in this system, not merely theoretical.

### Recommendation
Rewrite `unDelegateMaxUsage` in both `UnDelegateResourceActuator.execute` and `UnDelegateResourceProcessor.execute` to avoid dividing before multiplying, matching the pattern already applied in `ResourceProcessor.calculateGlobalLimitV2` / `RepositoryImpl.usageToBalance`:
```java
long unDelegateMaxUsage = BigInteger.valueOf(unDelegateBalance)
    .multiply(BigInteger.valueOf(dynamicStore.getTotalNetLimit()))
    .divide(BigInteger.valueOf(TRX_PRECISION)
        .multiply(BigInteger.valueOf(dynamicStore.getTotalNetWeight())))
    .longValueExact();
```
(and the analogous energy variant), ideally gated by the existing `allowHardenResourceCalculation`/`disableJavaLangMath()` feature flags used elsewhere for consistency and safe rollout, with equivalent hardened test coverage as `CalculateGlobalLimitHardenTest`.

### Proof of Concept
1. Delegate a large bandwidth balance from account A to account B, then set network parameters (`TotalNetLimit`, `TotalNetWeight`) such that `unDelegateBalance / TRX_PRECISION * totalLimit / totalWeight` loses precision under `double` arithmetic relative to the exact integer computation `(unDelegateBalance * totalLimit) / (TRX_PRECISION * totalWeight)` (e.g. large `unDelegateBalance` ~1e15–1e18 combined with non-power-of-two `totalNetWeight`, similar magnitudes used in `CalculateGlobalLimitHardenTest.testGlobalEnergyLimitV2CorrectVsDoublePrecisionLoss`).
2. Call `UnDelegateResourceContract` (or trigger the native `unDelegateResource` precompile from a contract) to undelegate part of the balance back from B to A.
3. Compare the resulting `unDelegateMaxUsage`/`transferUsage` applied to B's `netUsage`/`energyUsage` against the value computed via exact `BigInteger` multiplication-then-division; observe the divergence, demonstrating the transferred usage cap is miscalculated due to premature floating-point division.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/UnDelegateResourceActuator.java (L80-85)
```java
            // calculate usage
            long unDelegateMaxUsage = (long) ((double) unDelegateBalance / TRX_PRECISION
                * ((double) (dynamicStore.getTotalNetLimit()) / dynamicStore.getTotalNetWeight()));
            transferUsage = (long) (receiverCapsule.getNetUsage()
                * ((double) (unDelegateBalance) / receiverCapsule.getAllFrozenBalanceForBandwidth()));
            transferUsage = min(unDelegateMaxUsage, transferUsage);
```

**File:** actuator/src/main/java/org/tron/core/actuator/UnDelegateResourceActuator.java (L103-108)
```java
            // calculate usage
            long unDelegateMaxUsage = (long) ((double) unDelegateBalance / TRX_PRECISION
                * ((double) (dynamicStore.getTotalEnergyCurrentLimit()) / dynamicStore.getTotalEnergyWeight()));
            transferUsage = (long) (receiverCapsule.getEnergyUsage()
                * ((double) (unDelegateBalance) / receiverCapsule.getAllFrozenBalanceForEnergy()));
            transferUsage = min(unDelegateMaxUsage, transferUsage);
```

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/UnDelegateResourceProcessor.java (L115-120)
```java
            // calculate usage
            long unDelegateMaxUsage = (long) ((double) unDelegateBalance / TRX_PRECISION
                * dynamicStore.getTotalNetLimit() / repo.getTotalNetWeight());
            transferUsage = (long) (receiverCapsule.getNetUsage()
                * ((double) (unDelegateBalance) / receiverCapsule.getAllFrozenBalanceForBandwidth()));
            transferUsage = min(unDelegateMaxUsage, transferUsage, VMConfig.disableJavaLangMath());
```

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/UnDelegateResourceProcessor.java (L139-144)
```java
            // calculate usage
            long unDelegateMaxUsage = (long) ((double) unDelegateBalance / TRX_PRECISION
                * dynamicStore.getTotalEnergyCurrentLimit() / repo.getTotalEnergyWeight());
            transferUsage = (long) (receiverCapsule.getEnergyUsage()
                * ((double) (unDelegateBalance) / receiverCapsule.getAllFrozenBalanceForEnergy()));
            transferUsage = min(unDelegateMaxUsage, transferUsage, VMConfig.disableJavaLangMath());
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

**File:** actuator/src/main/java/org/tron/core/vm/repository/RepositoryImpl.java (L257-266)
```java
  private long usageToBalance(long usage, long totalWeight, long totalLimit) {
    if (hardenResourceCalculation()) {
      return BigInteger.valueOf(usage)
          .multiply(BigInteger.valueOf(totalWeight))
          .multiply(BigInteger.valueOf(TRX_PRECISION))
          .divide(BigInteger.valueOf(totalLimit))
          .longValueExact();
    }
    return (long) ((double) usage * totalWeight / totalLimit * TRX_PRECISION);
  }
```
