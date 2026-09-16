## Title
Division-by-zero in bandwidth/energy usage transfer during un-delegation corrupts resource accounting - (File: `actuator/src/main/java/org/tron/core/vm/nativecontract/UnDelegateResourceProcessor.java`)

### Summary
`UnDelegateResourceProcessor.execute()` (reachable via the TVM `unDelegateResource` native contract precompile) and its non-TVM twin `UnDelegateResourceActuator.execute()` (reachable via a plain `UnDelegateResourceContract` broadcast transaction) compute a `transferUsage` value by dividing the receiver's frozen balance for bandwidth/energy, without checking that the denominator is non-zero, analogous to the ImageMagick `resample.c` division-by-zero flaw (CVE-2021-20246) where an unchecked denominator in a resource-scaling calculation leads to undefined/incorrect results.

### Finding Description
In `UnDelegateResourceProcessor.execute()`: [1](#0-0) 
```java
transferUsage = (long) (receiverCapsule.getNetUsage()
    * ((double) (unDelegateBalance) / receiverCapsule.getAllFrozenBalanceForBandwidth()));
```
and the identical pattern for energy at lines 140-146, and equivalently in `UnDelegateResourceActuator.java` lines 81-85 and 104-107.

The `else` branch executing this division is only reached when `receiverCapsule.getAcquiredDelegatedFrozenV2BalanceForBandwidth() >= unDelegateBalance` — i.e., the accounting bookkeeping value is still non-zero/sufficient — while `getAllFrozenBalanceForBandwidth()` (the receiver's *actual* current total frozen-for-bandwidth balance, which can independently drop to zero, e.g. after the receiver fully unfreezes/withdraws or after a TVM contract self-destructs and is recreated as noted in the surrounding comments) can be `0`. Because the division is performed in `double` arithmetic, dividing by zero does not throw `ArithmeticException` — it silently produces `Infinity` or `NaN`. Casting `Infinity` to `long` yields `Long.MAX_VALUE`, and casting `NaN` yields `0`. This is the exact bug class described in the CVE report: an unguarded division whose denominator can become zero, producing undefined/garbage results rather than a controlled error.

### Impact Explanation
If `transferUsage` becomes `Long.MAX_VALUE` (from the `Infinity` case), the subsequent line: [2](#0-1) 
```java
long newNetUsage = receiverCapsule.getNetUsage() - transferUsage;
receiverCapsule.setNetUsage(newNetUsage);
```
subtracts a near-`Long.MAX_VALUE` value from `getNetUsage()`, causing signed 64-bit overflow and producing an arbitrary (potentially huge positive) `netUsage`/`energyUsage` value written back to on-chain account state. This corrupts the receiver's bandwidth/energy usage bookkeeping, which is a core resource-accounting invariant used throughout `BandwidthProcessor`/`EnergyProcessor` to gate free transaction throughput and energy consumption for every subsequent transaction/contract call from that account. A corrupted usage counter can either permanently deny the receiver resources it is entitled to, or (in the `min()` clamp path with `unDelegateMaxUsage`) skew accounting in the account's favor, undermining the resource-metering system that TVM execution and bandwidth consumption depend on network-wide.

### Likelihood Explanation
This code path is reached by any account issuing a standard, unprivileged `UnDelegateResourceContract` (via `UnDelegateResourceActuator`) or by any TVM contract invoking the `unDelegateResource` precompile (via `UnDelegateResourceProcessor`), both of which are broadcastable by any transaction sender without special privileges. Triggering the zero-denominator condition requires the receiver's `getAllFrozenBalanceForBandwidth()`/`getAllFrozenBalanceForEnergy()` to be `0` while its `getAcquiredDelegatedFrozenV2Balance...` bookkeeping value is still `>= unDelegateBalance` — a state explicitly anticipated by the code's own comments about contract self-destruct/re-creation and stale delegation records, making it plausible for an attacker to engineer via delegate/undelegate/freeze/unfreeze/suicide sequences. I was not able to fully trace every code path that can independently zero `getAllFrozenBalanceForBandwidth()` while leaving `getAcquiredDelegatedFrozenV2BalanceForBandwidth()` non-zero (this would require deeper tracing through `FreezeBalanceV2Actuator`, `UnfreezeBalanceV2Actuator`, and self-destruct handling), so the exact trigger sequence should be verified with a live/test-network reproduction before being treated as fully confirmed.

### Recommendation
Guard the division in both `UnDelegateResourceProcessor.execute()` and `UnDelegateResourceActuator.execute()`: check `receiverCapsule.getAllFrozenBalanceForBandwidth()`/`getAllFrozenBalanceForEnergy()` for zero (or non-positive) before dividing, and skip/zero out `transferUsage` in that case, mirroring the zero-check pattern already used in `EnergyProcessor.calculateGlobalEnergyLimit`/`calculateGlobalEnergyLimitV2` and `BandwidthProcessor.calculateGlobalNetLimit`/`calculateGlobalNetLimitV2` (`if (totalEnergyWeight == 0) { return 0; }`), and clamp results using `Math.min`/`addExact` style safe arithmetic to prevent silent overflow from `Infinity`/`NaN` casts.

### Proof of Concept
Not independently verified end-to-end due to tool/session limits; a concrete PoC would require constructing an account/receiver state where `getAcquiredDelegatedFrozenV2BalanceForBandwidth() >= unDelegateBalance` while `getAllFrozenBalanceForBandwidth() == 0` (e.g., via a sequence of `DelegateResourceContract` followed by full unfreeze or contract-suicide/re-creation of the receiver), then submitting an `UnDelegateResourceContract` for that receiver and inspecting the resulting `netUsage`/`energyUsage` field for overflow/corruption — this should be validated on a local testnet/Devin session before treating impact as confirmed.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/UnDelegateResourceProcessor.java (L110-123)
```java
          if (receiverCapsule.getAcquiredDelegatedFrozenV2BalanceForBandwidth()
              < unDelegateBalance) {
            // A TVM contract suicide, re-create will produce this situation
            receiverCapsule.setAcquiredDelegatedFrozenV2BalanceForBandwidth(0);
          } else {
            // calculate usage
            long unDelegateMaxUsage = (long) ((double) unDelegateBalance / TRX_PRECISION
                * dynamicStore.getTotalNetLimit() / repo.getTotalNetWeight());
            transferUsage = (long) (receiverCapsule.getNetUsage()
                * ((double) (unDelegateBalance) / receiverCapsule.getAllFrozenBalanceForBandwidth()));
            transferUsage = min(unDelegateMaxUsage, transferUsage, VMConfig.disableJavaLangMath());

            receiverCapsule.addAcquiredDelegatedFrozenV2BalanceForBandwidth(-unDelegateBalance);
          }
```

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/UnDelegateResourceProcessor.java (L125-127)
```java
          long newNetUsage = receiverCapsule.getNetUsage() - transferUsage;
          receiverCapsule.setNetUsage(newNetUsage);
          receiverCapsule.setLatestConsumeTime(now);
```
