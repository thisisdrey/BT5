## Finding

### Title
Floating-point-derived resource usage subtracted from exact on-chain frozen balance produces unreliable collateral-style check in `DelegateResourceActuator`/`DelegateResourceProcessor` - (File: `actuator/src/main/java/org/tron/core/actuator/DelegateResourceActuator.java`, `actuator/src/main/java/org/tron/core/vm/nativecontract/DelegateResourceProcessor.java`, `actuator/src/main/java/org/tron/core/vm/utils/FreezeV2Util.java`)

### Summary
The `DelegateResourceActuator.validate()` (broadcastable `DelegateResourceContract`) and the equivalent TVM native-contract path `DelegateResourceProcessor.validate()` (reachable from a contract call via the `delegateResource` precompiled/native opcode) both gate the amount an account may delegate by subtracting a *derived, floating-point-scaled estimate* of currently consumed resource (`v2NetUsage`/`v2EnergyUsage`) from an *exact* on-chain frozen balance (`getFrozenV2BalanceForBandwidth()`/`getFrozenV2BalanceForEnergy()`), exactly mirroring the reported bug class: two values computed through non-equivalent derivation paths are subtracted and compared as if they were directly comparable.

### Finding Description
In `DelegateResourceActuator.java`:
```java
long netUsage = (long) (accountNetUsage * TRX_PRECISION * ((double)
    (dynamicStore.getTotalNetWeight()) / dynamicStore.getTotalNetLimit()));
long v2NetUsage = getV2NetUsage(ownerCapsule, netUsage, this.disableJavaLangMath());
if (ownerCapsule.getFrozenV2BalanceForBandwidth() - v2NetUsage < delegateBalance) {
  throw new ContractValidateException(
      "delegateBalance must be less than or equal to available FreezeBandwidthV2 balance");
}
``` [1](#0-0) 

`netUsage` is a *double-precision, ratio-scaled estimate* of bandwidth consumption (`totalNetWeight`/`totalNetLimit` is a live, block-by-block-changing global ratio), not an exact accounting figure tied 1:1 to the account's frozen balance units. `getV2NetUsage` then subtracts three exact-integer quantities (own frozen balance, acquired delegated balances) from this floating estimate and clamps the result to a minimum of 0: [2](#0-1) 

The result (`v2NetUsage`) — itself already the product of subtracting an estimated value from exact balances — is then subtracted a second time from the exact `getFrozenV2BalanceForBandwidth()` to gate `delegateBalance`. Because `netUsage` depends on `totalNetWeight`/`totalNetLimit`, which fluctuate with every freeze/unfreeze/delegate across the entire network and are computed with floating-point division, the "available balance" figure used for the collateral-style check does not represent a value directly comparable to the account's real frozen balance. This is the same class of defect as the reported issue: `cache.borrowedAmount` (derived from one calculation domain) being subtracted from `cache.holdTokenBalance` (derived from a different, unrelated calculation domain) to gate a limit.

The identical pattern is duplicated in the TVM-reachable native contract path used when a smart contract calls the delegate-resource precompile: [3](#0-2) 

### Impact Explanation
Because `netUsage` is recomputed from a live, protocol-wide, floating-point ratio (`totalNetWeight/totalNetLimit`) rather than the account's own precise usage/frozen ledger, an attacker/normal user could observe network conditions (or influence them via their own freeze/unfreeze actions) where the estimate diverges from the true value in either direction:
- If `netUsage` (hence `v2NetUsage`) is under-estimated relative to true usage, `getFrozenV2BalanceForBandwidth() - v2NetUsage` overstates truly-free balance, allowing an account to delegate more bandwidth/energy resource than is actually free — i.e., double-committing already-consumed frozen collateral to a receiver, an unbacked resource allocation.
- Conversely an over-estimate would incorrectly block legitimate delegation (denial of a valid operation), a functional-correctness bug but lower severity.

The economically damaging direction (under-estimation permitting over-delegation) is the collateral-limit-bypass analog to the reported bug, and would let a caller create "phantom" delegated resource that is not fully backed by actually-idle frozen TRX, undermining resource accounting invariants used across bandwidth/energy consumption throughout the chain.

### Likelihood Explanation
Both `validate()` paths are reachable directly from a single signed `DelegateResourceContract` transaction, and from any smart contract executing the delegate-resource native/precompiled call, i.e. by any unprivileged account or contract deployer — no special privilege required. The floating-point ratio `totalNetWeight/totalNetLimit` changes with ordinary network activity that any user can also nudge with their own freeze/unfreeze transactions, making the divergence practically triggerable rather than purely theoretical.

### Recommendation
Replace the floating-point, network-wide ratio estimate used to compute `netUsage`/`energyUsage` inside the availability check with the account's own exact usage accounting (as already tracked via `BandwidthProcessor`/`EnergyProcessor` usage fields), or perform the comparison using consistent integer arithmetic scaled to the same unit basis, so the subtraction in `getFrozenV2BalanceForBandwidth() - v2NetUsage < delegateBalance` (and the ENERGY equivalent) only ever compares two quantities derived from the same accounting domain.

### Proof of Concept
1. Freeze balance for bandwidth on an account (`ownerCapsule.addFrozenBalanceForBandwidthV2`).
2. Drive real, unrelated network-wide freeze/unfreeze activity (or wait for other accounts' actions) so that `dynamicStore.getTotalNetWeight() / dynamicStore.getTotalNetLimit()` produces a ratio that under-represents the account's true bandwidth consumption fraction when multiplied by `accountNetUsage * TRX_PRECISION`.
3. Call `DelegateResourceContract` (or the equivalent TVM native call) requesting `delegateBalance` close to `getFrozenV2BalanceForBandwidth()` — the check at `actuator/src/main/java/org/tron/core/actuator/DelegateResourceActuator.java:166` passes because `v2NetUsage` is computed too low, even though the account's bandwidth is genuinely more consumed than the estimate reflects.
4. The delegation succeeds, over-committing frozen collateral that is still effectively in use for the owner's own bandwidth consumption, producing resource accounting inconsistent with actual on-chain frozen balances.

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

**File:** actuator/src/main/java/org/tron/core/vm/utils/FreezeV2Util.java (L245-252)
```java
  public static long getV2NetUsage(AccountCapsule ownerCapsule, long netUsage, boolean
      disableJavaLangMath) {
    long v2NetUsage= netUsage
        - ownerCapsule.getFrozenBalance()
        - ownerCapsule.getAcquiredDelegatedFrozenBalanceForBandwidth()
        - ownerCapsule.getAcquiredDelegatedFrozenV2BalanceForBandwidth();
    return max(0, v2NetUsage, disableJavaLangMath);
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/DelegateResourceProcessor.java (L63-71)
```java
        long netUsage = (long) (ownerCapsule.getNetUsage() * TRX_PRECISION * ((double)
            (repo.getTotalNetWeight()) / dynamicStore.getTotalNetLimit()));

        long v2NetUsage = getV2NetUsage(ownerCapsule, netUsage, disableJavaLangMath);

        if (ownerCapsule.getFrozenV2BalanceForBandwidth() - v2NetUsage < delegateBalance) {
          throw new ContractValidateException(
                  "delegateBalance must be less than or equal to available FreezeBandwidthV2 balance");
        }
```
