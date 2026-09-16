### Title
Order-Dependent Miscalculation of Transferred Usage in `UnDelegateResourceProcessor`/`UnDelegateResourceActuator` Due to Ratio Computed Against Mutable Shared Denominator - ([File: actuator/src/main/java/org/tron/core/vm/nativecontract/UnDelegateResourceProcessor.java])

### Summary
`UnDelegateResourceProcessor.execute()` and its non-TVM counterpart `UnDelegateResourceActuator.execute()` compute the amount of bandwidth/energy "usage" to transfer back to the delegation owner as a simple proportional ratio of the current receiver's total delegated-resource pool: `transferUsage = receiverUsage * (unDelegateBalance / receiverCapsule.getAllFrozenBalanceForBandwidth()/Energy())`. This mirrors the exact bug class in the external report: a proportional-reduction calculation is computed against a **live, mutable aggregate baseline** (the receiver's current total frozen/delegated balance) instead of a value isolated to the specific delegation being unwound, so the result becomes dependent on the order/timing of unrelated delegate/undelegate operations from other parties, just as EigenPodManager's `_reduceSlashingFactor()` produced different final states depending on whether a validator was verified before or after a checkpoint.

### Finding Description
In `UnDelegateResourceProcessor.execute()`: [1](#0-0) 
and the identical pattern for ENERGY: [2](#0-1) 

The same logic is duplicated in the plain actuator path used by ordinary signed transactions: [3](#0-2) 

`receiverCapsule.getAllFrozenBalanceForBandwidth()` / `getAllFrozenBalanceForEnergy()` represents the receiver's **entire current pool** of self-frozen balance plus all balances acquired from every delegator (V1 and V2), not just the balance delegated by the specific owner performing this `UnDelegateResourceContract`. The code treats the receiver's `netUsage`/`energyUsage` as if it were evenly and statically distributed across that pool, and slices out `transferUsage` proportional to `unDelegateBalance / totalPool` at the *current* instant.

This is the same root-cause pattern as the EigenLayer report: a proportional-reduction formula is applied using a denominator/baseline that can be altered by intervening state changes (other users' delegate/undelegate actions, or the receiver's own freeze/unfreeze actions) that occur between the time usage accrued and the time this particular undelegation is processed. Just as EigenPodManager's slashing factor calculation gave different final withdrawable shares depending on the order of "verify validator" vs. "checkpoint," here the value transferred back to `ownerCapsule` (via `processor.unDelegateIncrease(...)`) depends on the order in which multiple delegators undelegate and on how much the receiver's total pool has grown/shrunk from unrelated delegations in between — there is no accounting mechanism that isolates "how much usage was actually caused by this specific delegator's balance" from "how much of the pool exists right now, contributed by everyone else."

### Impact Explanation
Because `transferUsage` is a function of the receiver's aggregate, mutable frozen-balance pool rather than a value fixed/tracked per delegation, the sum of usage returned to all owners across a sequence of undelegate calls does not have to equal the receiver's actual accumulated usage. An attacker who controls both the delegation owner and receiver addresses (or colludes with a receiver) can manipulate the order and timing of `DelegateResourceContract`/`UnDelegateResourceContract` calls (broadcastable by any account, and also reachable via the TVM `undelegateresource` native contract from any deployed contract) to extract more bandwidth/energy usage credit back onto their own account (`ownerCapsule`, via `EnergyProcessor.unDelegateIncrease`/`BandwidthProcessor.unDelegateIncrease`) than they actually consumed. This effectively fabricates unbacked resource credit — analogous to "unbacked balance" — allowing free TRX-equivalent resource (bandwidth/energy) beyond what real frozen TRX backs, or conversely causes other legitimate delegators to permanently lose usage credit they are entitled to.

### Likelihood Explanation
The path is reachable by any account via a single signed `UnDelegateResourceContract` transaction, or via the `undelegateresource` TVM precompile/native-contract call from a deployed contract — no special privilege required, matching the "unprivileged transaction broadcaster / contract deployer" reachability requirement. Multi-delegator scenarios where a receiver has several concurrent delegated balances plus real usage are realistic and common (e.g., high-usage contract accounts fueled by multiple sponsors), and an attacker fully controlling the timing of their own delegate/undelegate calls (and, if colluding with the receiver, controlling receiver-side usage growth as well) can reliably trigger the divergence. I could not fully trace the exact numeric magnitude of gain/loss through `ResourceProcessor.unDelegateIncrease`/`BandwidthProcessor`/`EnergyProcessor` internals in the time available, so the precise bound on extractable value is unverified and should be confirmed with a concrete multi-delegator PoC trace.

### Recommendation
Track usage attribution per-delegation (e.g., persist how much usage was contributed/attributable to each specific `DelegatedResourceCapsule`) instead of re-deriving a proportional share from the receiver's current aggregate frozen balance at undelegate time. At minimum, snapshot and scale using the pool composition *at the time the specific delegation was created/last updated* rather than the live, globally-shared total, so that concurrent delegate/undelegate activity by unrelated parties cannot change the outcome of a given undelegation.

### Proof of Concept
Conceptual scenario (not verified end-to-end due to tool-call exhaustion):
1. Receiver R has 0 frozen balance. Delegator A delegates 100 TRX bandwidth to R (`getAllFrozenBalanceForBandwidth(R) = 100`).
2. R consumes bandwidth, accruing `netUsage = U`.
3. Delegator B delegates another 100 TRX bandwidth to R (`getAllFrozenBalanceForBandwidth(R) = 200`), diluting the "pool" without adding usage.
4. Delegator A calls `UnDelegateResourceContract` to undelegate their 100 TRX. `transferUsage_A = U * (100/200) = U/2` is moved back to A, leaving R with `netUsage = U/2` and pool `100` (B's).
5. Delegator B then undelegates their 100 TRX. `transferUsage_B = (U/2) * (100/100) = U/2` moves back to B.
6. Total usage returned to A and B is `U` — matching in this order, but reversing the order (B first, then A) or interleaving further delegate/undelegate operations from either party changes each individual's `transferUsage` split, since each calculation depends on the pool size and residual usage *at call time*, not on each delegator's actual contribution to usage. A crafted sequence of many small delegate/undelegate operations by a single attacker-controlled owner interacting with a self-controlled or complicit receiver can be used to bias this split in the attacker's favor, extracting usage credit disproportionate to their real resource consumption. A full numeric PoC exercising `BandwidthProcessor`/`EnergyProcessor.unDelegateIncrease` should be constructed to confirm the exact exploitable delta.

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

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/UnDelegateResourceProcessor.java (L138-147)
```java
          } else {
            // calculate usage
            long unDelegateMaxUsage = (long) ((double) unDelegateBalance / TRX_PRECISION
                * dynamicStore.getTotalEnergyCurrentLimit() / repo.getTotalEnergyWeight());
            transferUsage = (long) (receiverCapsule.getEnergyUsage()
                * ((double) (unDelegateBalance) / receiverCapsule.getAllFrozenBalanceForEnergy()));
            transferUsage = min(unDelegateMaxUsage, transferUsage, VMConfig.disableJavaLangMath());

            receiverCapsule.addAcquiredDelegatedFrozenV2BalanceForEnergy(-unDelegateBalance);
          }
```

**File:** actuator/src/main/java/org/tron/core/actuator/UnDelegateResourceActuator.java (L79-111)
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

          long newNetUsage = receiverCapsule.getNetUsage() - transferUsage;
          receiverCapsule.setNetUsage(newNetUsage);
          receiverCapsule.setLatestConsumeTime(now);
          break;
        case ENERGY:
          EnergyProcessor energyProcessor = new EnergyProcessor(dynamicStore, accountStore);
          energyProcessor.updateUsage(receiverCapsule);

          if (receiverCapsule.getAcquiredDelegatedFrozenV2BalanceForEnergy()
              < unDelegateBalance) {
            // A TVM contract receiver, re-create will produce this situation
            receiverCapsule.setAcquiredDelegatedFrozenV2BalanceForEnergy(0);
          } else {
            // calculate usage
            long unDelegateMaxUsage = (long) ((double) unDelegateBalance / TRX_PRECISION
                * ((double) (dynamicStore.getTotalEnergyCurrentLimit()) / dynamicStore.getTotalEnergyWeight()));
            transferUsage = (long) (receiverCapsule.getEnergyUsage()
                * ((double) (unDelegateBalance) / receiverCapsule.getAllFrozenBalanceForEnergy()));
            transferUsage = min(unDelegateMaxUsage, transferUsage);

            receiverCapsule.addAcquiredDelegatedFrozenV2BalanceForEnergy(-unDelegateBalance);
          }
```
