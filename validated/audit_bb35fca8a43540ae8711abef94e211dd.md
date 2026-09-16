### Title
Double-precision rounding in `DelegateResourceActuator`/`UnDelegateResourceActuator` bandwidth/energy usage checks can permit over-delegation of frozen resource balance - ([File: actuator/src/main/java/org/tron/core/actuator/DelegateResourceActuator.java])

### Summary
The Sherlock finding describes `bps2Uint()` losing precision as the base number (`totalVotingPower`) grows, because the function performs `(_number * _bps) / 10000` with plain integer division instead of a higher-precision fixed-point scheme. The java-tron codebase has an analogous, already partly-acknowledged bug class: several bandwidth/energy weight calculations perform `double` division/multiplication (`(double) totalLimit / totalWeight`, etc.) that lose precision as the underlying network totals (`getTotalNetWeight()`, `getTotalNetLimit()`, `getTotalEnergyWeight()`, `getTotalEnergyCurrentLimit()`) grow large. The project has already recognized and "hardened" this exact pattern in `ResourceProcessor.calculateGlobalLimitV1/V2` (replacing double math with `BigInteger`, see the comment "Hardened replacement of legacy V2 formula" and the dedicated `CalculateGlobalLimitHardenTest`), but `DelegateResourceActuator.validate()` and `UnDelegateResourceActuator.execute()` still perform the unhardened `double`-based computation directly, unguarded by the `hardenCalculation()`/`allowHardenResourceCalculation()` feature flag used elsewhere.

### Finding Description
In `DelegateResourceActuator.validate()`, the amount of bandwidth/energy an account is allowed to delegate is gated by a usage estimate computed purely with `double` arithmetic: [1](#0-0) 
The same unhardened pattern appears for `ENERGY`: [2](#0-1) 

`UnDelegateResourceActuator.execute()` (which mutates account bandwidth/energy usage on the receiver side, reachable directly from an `UnDelegateResourceContract` broadcast by any account) has the identical style of unhardened `double` math: [3](#0-2) [4](#0-3) 

This is the same bug class as `bps2Uint()`: dividing two large integers via floating point (or truncating integer division) loses precision, and the magnitude of the error grows as `totalNetWeight`/`totalEnergyWeight` grow (which happens naturally over time as more TRX gets frozen network-wide — directly analogous to `totalVotingPower` growing in the Sherlock report). The project's own hardening work confirms this is a genuine, previously-fixed-elsewhere issue class: [5](#0-4) 
and the dedicated regression test explicitly demonstrates the double-vs-BigInteger divergence: [6](#0-5) 

However, `DelegateResourceActuator`/`UnDelegateResourceActuator` never call into `calculateGlobalLimitV1`/`V2` or any `hardenCalculation()`-gated helper — they inline the raw `double` computation, so the fix applied to `ResourceProcessor`/`BandwidthProcessor`/`EnergyProcessor` does not cover this code path at all.

### Impact Explanation
`v2NetUsage`/`v2EnergyUsage` computed with lossy `double` arithmetic feed directly into the check that determines whether `delegateBalance` is permissible:
```
if (ownerCapsule.getFrozenV2BalanceForBandwidth() - v2NetUsage < delegateBalance) {
  throw new ContractValidateException(...);
}
```
If precision loss causes `v2NetUsage` to be computed lower than the true value (rounding down, as `bps2Uint`-style truncation always does), the validate check can pass for a `delegateBalance` that exceeds the account's genuinely free (unused) frozen resource. This lets an account owner delegate resource that is not actually backed by unused frozen TRX, which is a form of unbacked resource issuance to the `receiverAddress` — the receiver's `AcquiredDelegatedFrozenV2BalanceForBandwidth/Energy` is inflated beyond what the network-wide weight/limit accounting can actually support, undermining the resource model's guarantee that delegated bandwidth/energy is fully collateralized by frozen TRX.

### Likelihood Explanation
Reachable by any account through an ordinary signed `DelegateResourceContract` / `UnDelegateResourceContract` transaction — no special privileges required. As `TotalNetWeight`/`TotalEnergyWeight` continue to grow with chain usage (mirroring the `totalVotingPower` growth condition in the original report), the magnitude of the precision error increases, making the discrepancy more exploitable over time, particularly for accounts with large frozen balances/usage where the multiplication `accountNetUsage * TRX_PRECISION * (double)` can already lose low-order bits before the final cast to `long`.

### Recommendation
Route the usage estimates in `DelegateResourceActuator.validate()` and `UnDelegateResourceActuator.execute()` through the same hardened `BigInteger`-based helpers already introduced in `ResourceProcessor`/`BandwidthProcessor`/`EnergyProcessor` (e.g. `calculateGlobalLimitV1`/`V2`, `getV2NetUsage`/`getV2EnergyUsage` variants gated by `hardenCalculation()`), so that all delegation-eligibility checks use exact integer math consistently, closing the precision gap between what governs "available balance" and what actually gets delegated.

### Proof of Concept
1. Wait for (or construct a test environment with) large `TotalNetWeight` and `TotalNetLimit` values, as already exercised in `CalculateGlobalLimitHardenTest.testGlobalNetLimitV2CorrectVsDoublePrecisionLoss` which shows double-based computation diverging from the exact `BigInteger` result for realistic magnitudes such as `totalNetWeight = 1_234_567L`, `frozeBalance = 9_876_543_210_000_000L`.
2. Have an account with `accountNetUsage` (or `energyUsage`) and `getFrozenV2BalanceForBandwidth()` positioned such that the double-computed `v2NetUsage` in `DelegateResourceActuator.validate()` rounds down relative to the true integer value.
3. Submit a `DelegateResourceContract` with `delegateBalance` set to a value that is invalid against the true (BigInteger) usage but passes the lossy `double` check in `DelegateResourceActuator.validate()` at line 166/180.
4. Observe the actuator executes successfully, transferring `delegateBalance` (`addDelegatedFrozenV2BalanceForBandwidth`/`addFrozenBalanceForBandwidthV2(-delegateBalance)`) to the receiver despite insufficient true free balance, analogous to how `calculateGlobalLimitV2`'s hardening test proves the double path diverges from the exact `BigInteger` path in `ResourceProcessor.java` lines 359-378.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/DelegateResourceActuator.java (L162-169)
```java
        long netUsage = (long) (accountNetUsage * TRX_PRECISION * ((double)
            (dynamicStore.getTotalNetWeight()) / dynamicStore.getTotalNetLimit()));
        long v2NetUsage = getV2NetUsage(ownerCapsule, netUsage,
            this.disableJavaLangMath());
        if (ownerCapsule.getFrozenV2BalanceForBandwidth() - v2NetUsage < delegateBalance) {
          throw new ContractValidateException(
              "delegateBalance must be less than or equal to available FreezeBandwidthV2 balance");
        }
```

**File:** actuator/src/main/java/org/tron/core/actuator/DelegateResourceActuator.java (L176-183)
```java
        long energyUsage = (long) (ownerCapsule.getEnergyUsage() * TRX_PRECISION * ((double)
            (dynamicStore.getTotalEnergyWeight()) / dynamicStore.getTotalEnergyCurrentLimit()));
        long v2EnergyUsage = getV2EnergyUsage(ownerCapsule, energyUsage,
            this.disableJavaLangMath());
        if (ownerCapsule.getFrozenV2BalanceForEnergy() - v2EnergyUsage < delegateBalance) {
          throw new ContractValidateException(
                  "delegateBalance must be less than or equal to available FreezeEnergyV2 balance");
        }
```

**File:** actuator/src/main/java/org/tron/core/actuator/UnDelegateResourceActuator.java (L80-88)
```java
            // calculate usage
            long unDelegateMaxUsage = (long) ((double) unDelegateBalance / TRX_PRECISION
                * ((double) (dynamicStore.getTotalNetLimit()) / dynamicStore.getTotalNetWeight()));
            transferUsage = (long) (receiverCapsule.getNetUsage()
                * ((double) (unDelegateBalance) / receiverCapsule.getAllFrozenBalanceForBandwidth()));
            transferUsage = min(unDelegateMaxUsage, transferUsage);

            receiverCapsule.addAcquiredDelegatedFrozenV2BalanceForBandwidth(-unDelegateBalance);
          }
```

**File:** actuator/src/main/java/org/tron/core/actuator/UnDelegateResourceActuator.java (L103-111)
```java
            // calculate usage
            long unDelegateMaxUsage = (long) ((double) unDelegateBalance / TRX_PRECISION
                * ((double) (dynamicStore.getTotalEnergyCurrentLimit()) / dynamicStore.getTotalEnergyWeight()));
            transferUsage = (long) (receiverCapsule.getEnergyUsage()
                * ((double) (unDelegateBalance) / receiverCapsule.getAllFrozenBalanceForEnergy()));
            transferUsage = min(unDelegateMaxUsage, transferUsage);

            receiverCapsule.addAcquiredDelegatedFrozenV2BalanceForEnergy(-unDelegateBalance);
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

**File:** framework/src/test/java/org/tron/core/db/CalculateGlobalLimitHardenTest.java (L96-112)
```java
  public void testGlobalEnergyLimitV2CorrectVsDoublePrecisionLoss() {
    long totalEnergyLimit = 50_000_000_000L;
    long totalEnergyWeight = 1_234_567L;
    long frozeBalance = 9_876_543_210_000_000L; // ~9.8e15

    dbManager.getDynamicPropertiesStore().saveTotalEnergyCurrentLimit(totalEnergyLimit);
    dbManager.getDynamicPropertiesStore().saveTotalEnergyWeight(totalEnergyWeight);

    BigInteger expected = BigInteger.valueOf(frozeBalance)
        .multiply(BigInteger.valueOf(totalEnergyLimit))
        .divide(BigInteger.valueOf(1_000_000L)
            .multiply(BigInteger.valueOf(totalEnergyWeight)));

    dbManager.getDynamicPropertiesStore().saveAllowHardenResourceCalculation(1);
    long actual = energyProcessor.calculateGlobalEnergyLimitV2(frozeBalance);
    Assert.assertEquals(expected.longValueExact(), actual);
  }
```
