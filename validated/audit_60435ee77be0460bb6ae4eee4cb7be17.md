### Title
Incorrect proportional-reduction basis when unDelegating resources allows a delegatee to retain excess bandwidth/energy usage - ([File: actuator/src/main/java/org/tron/core/actuator/UnDelegateResourceActuator.java])

### Summary
Both `UnDelegateResourceActuator.execute()` and its VM-native counterpart `UnDelegateResourceProcessor.execute()` compute the amount of usage (`transferUsage`) that must be clawed back from a resource receiver when a delegator un-delegates part of a delegation. The reduction ratio is built from the receiver's *aggregate* frozen balance rather than from the specific delegation being withdrawn, mirroring the Hyperdrive bug class where a proportional-reduction factor is derived from and/or compared against the wrong reference basis, so the "curve"/derived quantity is not scaled down by the correct amount.

### Finding Description
In `UnDelegateResourceActuator.execute()`: [1](#0-0) 

the code computes:
```
transferUsage = (long) (receiverCapsule.getNetUsage()
    * ((double) (unDelegateBalance) / receiverCapsule.getAllFrozenBalanceForBandwidth()));
transferUsage = min(unDelegateMaxUsage, transferUsage);
```
`receiverCapsule.getAllFrozenBalanceForBandwidth()` returns the receiver's *total* bandwidth-granting balance (self-frozen + all delegated-in balances, both V1 and V2), not the balance of the single delegation identified by `(ownerAddress, receiverAddress)` that is being partially/fully un-delegated. The same pattern is repeated for `ENERGY` and duplicated verbatim in the VM path: [2](#0-1) 

This is structurally the same bug class as the Hyperdrive report: a proportional-reduction factor (`unDelegateBalance / totalBasis`) is applied to a derived quantity (`transferUsage`, analogous to `shareReservesDelta`) using the *wrong reference basis* (aggregate frozen balance across all delegators, analogous to `_initialSharePrice`) instead of the basis specific to the delegation relationship being closed (analogous to `_openSharePrice`). When a receiver has multiple concurrent delegators, un-delegating from one delegator computes `transferUsage` as a fraction of the receiver's *entire* pool of frozen-in balance rather than of that specific delegator's contribution, so the usage clawed back does not correctly correspond to the balance actually being withdrawn.

Because `unDelegateMaxUsage` is computed independently from global network limits (`getTotalNetLimit()/getTotalNetWeight()`), the `min()` cap can mask the discrepancy in some cases, but whenever `transferUsage` (computed with the wrong denominator) is smaller than the cap, the receiver keeps disproportionately more/less of its consumed bandwidth/energy usage than it should relative to the balance actually being un-delegated by the specific owner, and the un-delegating owner does not correctly receive the usage instead via `processor.unDelegateIncrease(ownerCapsule, receiverCapsule, transferUsage, ...)`.

### Impact Explanation
This affects bandwidth/energy accounting rather than direct token balances, but resource accounting errors on java-tron directly translate into unbacked free-transaction capacity (bandwidth) or free contract execution (energy), which is economically equivalent to unbacked balance/theft of network resources: a delegatee receiving delegations from multiple owners could retain usage credit disproportionate to their currently backing frozen balance, allowing them to consume more bandwidth/energy than their remaining frozen TRX entitles them to, at the expense of other network participants' resource pools (`TotalNetLimit`/`TotalEnergyCurrentLimit`).

### Likelihood Explanation
Reachable by any account that has delegated resources to a receiver who has received delegations from more than one delegator (a common, legitimate multi-delegator scenario), simply by calling `UnDelegateResourceContract` (or the `unDelegateResource` native TVM function) for a partial amount. No special privilege is required — it is a plain user-facing actuator/precompile path.

### Recommendation
Compute `transferUsage` using the ratio of `unDelegateBalance` to the frozen balance specifically attributable to the `(ownerAddress → receiverAddress)` delegation relationship (i.e., use the corresponding `DelegatedResourceCapsule`'s stored frozen balance as the denominator, consistent with the validation checks in `validate()` at lines 271-298 of `UnDelegateResourceActuator.java`), rather than the receiver's aggregate `getAllFrozenBalanceForBandwidth()/getAllFrozenBalanceForEnergy()`, so the proportional usage clawback is anchored to the correct, delegation-specific basis.

### Proof of Concept
Conceptual scenario (not independently executed, derived from code reading only):
1. Delegator A delegates 1,000,000 sun for BANDWIDTH to Receiver R.
2. Delegator B delegates 9,000,000 sun for BANDWIDTH to the same Receiver R.
3. Receiver R consumes bandwidth so that `getNetUsage()` is large relative to `getAllFrozenBalanceForBandwidth()` (10,000,000 total).
4. Delegator A calls `UnDelegateResourceContract` to withdraw their full 1,000,000 sun.
5. `transferUsage` is computed as `netUsage * (1,000,000 / 10,000,000)` — i.e., 10% of R's total usage — regardless of how much of that usage was actually attributable to A's specific 1,000,000 sun contribution versus B's 9,000,000 sun contribution, so the usage returned to A (via `unDelegateIncrease`) is decoupled from A's actual share, and R's residual usage after B's larger delegation remains is not consistently proportional to R's remaining backing balance.

**Uncertainty**: I was not able to run this scenario against the actual chain state/tests within this session; this analysis is based solely on static code reading of `UnDelegateResourceActuator.java` and `UnDelegateResourceProcessor.java`. A background Devin session with test execution access would be needed to confirm the exact numeric divergence and whether existing test coverage (e.g. in `UnDelegateResourceActuatorTest`) already exercises/masks this multi-delegator scenario.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/UnDelegateResourceActuator.java (L79-88)
```java
          } else {
            // calculate usage
            long unDelegateMaxUsage = (long) ((double) unDelegateBalance / TRX_PRECISION
                * ((double) (dynamicStore.getTotalNetLimit()) / dynamicStore.getTotalNetWeight()));
            transferUsage = (long) (receiverCapsule.getNetUsage()
                * ((double) (unDelegateBalance) / receiverCapsule.getAllFrozenBalanceForBandwidth()));
            transferUsage = min(unDelegateMaxUsage, transferUsage);

            receiverCapsule.addAcquiredDelegatedFrozenV2BalanceForBandwidth(-unDelegateBalance);
          }
```

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/UnDelegateResourceProcessor.java (L115-123)
```java
            // calculate usage
            long unDelegateMaxUsage = (long) ((double) unDelegateBalance / TRX_PRECISION
                * dynamicStore.getTotalNetLimit() / repo.getTotalNetWeight());
            transferUsage = (long) (receiverCapsule.getNetUsage()
                * ((double) (unDelegateBalance) / receiverCapsule.getAllFrozenBalanceForBandwidth()));
            transferUsage = min(unDelegateMaxUsage, transferUsage, VMConfig.disableJavaLangMath());

            receiverCapsule.addAcquiredDelegatedFrozenV2BalanceForBandwidth(-unDelegateBalance);
          }
```
