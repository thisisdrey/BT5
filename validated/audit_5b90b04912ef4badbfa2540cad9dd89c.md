## Title
Truncated `unDelegateResource` usage-transfer calculation rounds down, permanently locking bandwidth/energy quota on repeated partial undelegations - (File: actuator/src/main/java/org/tron/core/vm/nativecontract/UnDelegateResourceProcessor.java / actuator/src/main/java/org/tron/core/actuator/UnDelegateResourceActuator.java)

### Summary
`UnDelegateResourceActuator`/`UnDelegateResourceProcessor` compute `transferUsage` — the portion of a receiver's consumed bandwidth/energy "usage window" that must move back to the delegator when a delegation is partially withdrawn — using `double` arithmetic cast to `long`, which truncates toward zero (rounds down) rather than rounding to the nearest or ceiling value. This is structurally the same rounding-direction defect as the reported `RedemptionPool.receiptToBurn` bug: an amount derived from a ratio of "remaining/consumed" quantities is rounded down instead of up, so every partial-fill operation (here, partial `unDelegateResource` calls) leaves a small residual amount unaccounted for at the receiver, which can be repeated to accumulate drift.

### Finding Description
In `UnDelegateResourceProcessor.execute` (and identically in `UnDelegateResourceActuator.execute`), when an account partially reclaims delegated BANDWIDTH/ENERGY from a receiver, the code computes: [1](#0-0) 

```java
long unDelegateMaxUsage = (long) ((double) unDelegateBalance / TRX_PRECISION
    * dynamicStore.getTotalNetLimit() / repo.getTotalNetWeight());
transferUsage = (long) (receiverCapsule.getNetUsage()
    * ((double) (unDelegateBalance) / receiverCapsule.getAllFrozenBalanceForBandwidth()));
transferUsage = min(unDelegateMaxUsage, transferUsage, VMConfig.disableJavaLangMath());
```

The equivalent block exists for ENERGY, and an identical pattern lives in the non-native actuator path: [2](#0-1) 

`(long)` casting of a `double` result truncates toward zero rather than rounding — this is the same "round down instead of up (or to nearest)" defect flagged in the external report for `receiptToBurn`. The consequence:
- `receiverCapsule.getNetUsage()` (or energy usage) is reduced by `transferUsage`, which is a slight under-count of the usage proportionally attributable to the balance being reclaimed.
- The owner (delegator) gets back `unDelegateBalance` worth of frozen resource, but the "usage" tracked against that resource is not moved back in full.
- Because `unDelegateResource` can be called repeatedly with small `unDelegateBalance` amounts (partial reclaim), the truncation error accumulates across calls, similar to how the reported bug accumulates across repeated partial redemption fills.

This differs slightly from the RedemptionPool case in that no tokens are literally "burned less than consumed," but the direction and mechanism of the bug — a ratio-based partial-settlement quantity computed via floor/truncating division instead of rounding up, invoked repeatedly by an ordinary user transaction (`UnDelegateResourceContract`, reachable via a signed transaction or the `UnDelegateResource` TVM precompile) — is analogous.

### Impact Explanation
Bandwidth/energy usage windows determine how much free bandwidth/energy an account can still consume before hitting quota limits. If `transferUsage` is systematically under-computed on partial `unDelegateResource` calls:
- The receiver's `netUsage`/`energyUsage` counters remain slightly inflated relative to the resource actually still backing them, and the owner's counters are correspondingly slightly deflated.
- Repeated partial undelegations (an attacker can freely choose to undelegate in many small increments instead of one large one, since `unDelegateBalance` is fully user-controlled and there is no minimum-increment restriction visible in `UnDelegateResourceProcessor.validate`) let an attacker deliberately maximize the truncation loss over many transactions.
- Over time this skews the bandwidth/energy accounting state (`netUsage`, `windowSize`) away from the true backing collateral, which can let the receiver or owner consume more resource than their remaining frozen balance justifies for a period, or conversely permanently strand a fraction of usage credit that nobody can reclaim — a form of resource-accounting drift analogous to "unbacked balance" for bandwidth/energy.

### Likelihood Explanation
Any account can call `UnDelegateResourceContract` directly (an ordinary signed transaction) or trigger the equivalent path via the TVM `unDelegateResource` precompile from a smart contract, entirely within the reachable "order placer / contract deployer" surface. Splitting a delegation reclaim into many small `unDelegateBalance` amounts is trivial and costs only the ordinary transaction fee, so the likelihood of an attacker being able to trigger and accumulate this rounding effect is high; the main uncertainty is the magnitude of the accumulated drift per call, which depends on `receiverCapsule.getAllFrozenBalanceForBandwidth()`/`getEnergyWeight()` at call time and could be very small per call (sub-unit), requiring many repeated calls to produce a materially exploitable amount.

### Recommendation
Replace the truncating `(long) (double * ratio)` computation of `transferUsage` (and `unDelegateMaxUsage`) with an exact, rounding-aware calculation — e.g., using `BigInteger`/`BigDecimal` multiplication followed by explicit `RoundingMode.CEILING` (or, if the direction should favor the protocol/receiver rather than the caller, choose the rounding mode that guarantees at least as much usage is transferred as is proportionally owed) instead of implicit floor/truncation via `double`-to-`long` narrowing. The `hardenCalculation()` path already used in `ResourceProcessor.unDelegateIncreaseV2` (`divideCeilExact` on `BigInteger`) is a precedent in this codebase for this exact issue and should be applied consistently to the `UnDelegateResourceProcessor`/`UnDelegateResourceActuator` usage-transfer calculations.

### Proof of Concept
1. Delegate a bandwidth/energy balance from account A (owner) to account B (receiver) via `DelegateResourceContract`.
2. Have account B consume bandwidth/energy so that `receiverCapsule.getNetUsage()` (or `getEnergyUsage()`) is non-zero.
3. Account A repeatedly calls `UnDelegateResourceContract` with a small `unDelegateBalance` (e.g., the minimum non-zero amount) many times instead of a single large undelegation.
4. Because `transferUsage = (long) (receiverCapsule.getNetUsage() * ((double) unDelegateBalance / receiverCapsule.getAllFrozenBalanceForBandwidth()))` truncates on every call, the sum of `transferUsage` across all partial calls will be strictly less than the single-call `transferUsage` that would result from undelegating the full amount at once (verifiable by comparing `sum_of_transferUsage(many small calls)` vs `transferUsage(one big call)` under otherwise identical starting state).
5. This measurable discrepancy demonstrates the accumulation effect; a background Devin agent with test-harness access (e.g., extending `framework/src/test/java/org/tron/common/runtime/vm/FreezeV2Test.java`, which already exercises `unDelegateResource`) should be used to instrument and quantify the exact drift in `netUsage`/`energyUsage` state after N partial undelegations versus 1 full undelegation, to confirm the magnitude of the accounting error.

**Note on completeness:** I could not fully trace whether `checkUndelegateResource` in `FreezeV2Util.java` (also truncation-based, used from `PrecompiledContracts.java`) is a completely separate, additional instance of the same rounding issue, or whether it feeds into the same code path already covered above — this would benefit from further investigation with full file/call-graph access.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/UnDelegateResourceProcessor.java (L114-123)
```java
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
