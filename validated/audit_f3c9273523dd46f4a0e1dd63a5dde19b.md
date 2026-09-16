### Title
Incorrect field returned in `DelegatedResourceCapsule.getExpireTimeForEnergy(DynamicPropertiesStore)` allows premature or blocked unfreeze of delegated ENERGY resources - (File: `chainbase/src/main/java/org/tron/core/capsule/DelegatedResourceCapsule.java`)

### Summary
`DelegatedResourceCapsule.getExpireTimeForEnergy(DynamicPropertiesStore)` is a two-branch method meant to return the expire time for a delegated ENERGY resource. When `AllowMultiSign` is disabled, the "else" branch that should return `getExpireTimeForEnergy()` instead returns `getExpireTimeForBandwidth()` — the same copy/paste class of bug (wrong sibling field/variable used in one branch of a multi-branch time calculation) described in the referenced RealityCards `_collectRentAction` finding.

### Finding Description [1](#0-0) 

```java
public long getExpireTimeForEnergy(DynamicPropertiesStore dynamicPropertiesStore) {
    if (dynamicPropertiesStore.getAllowMultiSign() == 0) {
      return this.delegatedResource.getExpireTimeForBandwidth();
    } else {
      return this.delegatedResource.getExpireTimeForEnergy();
    }
}
```

This is the exact bug pattern: a set of parallel branches each supposed to compute/return a value for the same conceptual quantity (here, "expire time for the ENERGY delegation"), but one branch (`AllowMultiSign == 0`) erroneously reads a different, unrelated field (`ExpireTimeForBandwidth`) instead of the intended `ExpireTimeForEnergy`. This mirrors the reported RealityCards CASE 6 bug where one branch used `block.timestamp` instead of the sibling variable used in the analogous branches.

This method is called from `UnfreezeBalanceActuator.validate()` to gate whether an ENERGY delegated-resource unfreeze is allowed: [2](#0-1) 

```java
case ENERGY:
  if (delegatedResourceCapsule.getFrozenBalanceForEnergy() <= 0) {
    throw new ContractValidateException("no delegateFrozenBalance(Energy)");
  }
  ...
  if (delegatedResourceCapsule.getExpireTimeForEnergy(dynamicStore) > now) {
    throw new ContractValidateException("It's not time to unfreeze.");
  }
  break;
```

Because the check reads `getExpireTimeForBandwidth()` instead of the correct `ExpireTimeForEnergy` field whenever `AllowMultiSign == 0`, the outcome of the unfreeze-time check is entirely determined by an unrelated field (the BANDWIDTH delegation's expire time on the same `DelegatedResourceCapsule` record), rather than the ENERGY delegation's own expire time.

### Impact Explanation
Two divergent unauthorized outcomes are possible depending on the relative values of the (unrelated) bandwidth expire time vs. the actual energy expire time:
1. If the BANDWIDTH expire time on the same delegated-resource record is already in the past (or zero/never set) while the real ENERGY expire time is still in the future, the check incorrectly passes, letting an owner reclaim (unfreeze) ENERGY resources still delegated to a receiver before the intended lock/expire period ends. This directly breaks the resource-delegation invariant that the receiver is entitled to use delegated ENERGY until the recorded expire time, potentially disrupting the receiver's expected resource availability and letting the owner "double-dip" (regain frozen TRX while the receiver still nominally holds the acquired ENERGY balance until it's separately reconciled) — an unauthorized account operation on committed resource state.
2. Conversely, if the BANDWIDTH expire time is still in the future while the ENERGY delegation has actually already expired, the unfreeze is wrongly blocked, freezing funds that should be withdrawable, causing (at minimum) resource lock-up beyond the intended period.

### Likelihood Explanation
This code path is reached by any account holder who froze balance with a receiver address for ENERGY delegation via `FreezeBalanceContract` (the legacy pre-`FreezeBalanceV2` delegation flow) and later submits an `UnfreezeBalanceContract` for the ENERGY resource type — a standard, unprivileged, single-signed transaction that any account can broadcast (`UnfreezeBalanceActuator`, reachable via `Wallet`/HTTP/gRPC broadcast). The bug's practical triggering additionally depends on the current value of `AllowMultiSign` (a chain parameter controlled by committee proposal) being `0`. Whether that condition still holds on the live production chain configuration cannot be determined from the code alone; this needs to be confirmed against the actual deployed `AllowMultiSign` proposal state before treating this as immediately exploitable.

### Recommendation
Fix the erroneous branch so it returns the correct field regardless of the `AllowMultiSign` flag (or remove the flag-based branching entirely if it is legacy/dead logic no longer needed):

```java
public long getExpireTimeForEnergy(DynamicPropertiesStore dynamicPropertiesStore) {
    return this.delegatedResource.getExpireTimeForEnergy();
}
```
or, if the `AllowMultiSign == 0` branch was intentionally meant to preserve some pre-fork compatibility behavior, verify the original intent against git history/changelogs and correct the field reference so both branches consistently reference ENERGY-related state.

### Proof of Concept
Conceptual reproduction (cannot execute against a live chain within this analysis; needs a background agent/test environment to confirm):
1. Deploy/attach to a java-tron node where the dynamic parameter `AllowMultiSign` equals `0`.
2. Account A calls `FreezeBalanceContract` with `resource = ENERGY` and a non-empty `receiver_address` = Account B, `frozen_duration = N` days, creating a `DelegatedResourceCapsule` with `ExpireTimeForEnergy = now + N days` while `ExpireTimeForBandwidth` on that same record remains `0` (never set, since only ENERGY was delegated).
3. Immediately (before `N` days elapse) Account A calls `UnfreezeBalanceContract` with `resource = ENERGY`, `receiver_address = B`.
4. In `UnfreezeBalanceActuator.validate()`, `delegatedResourceCapsule.getExpireTimeForEnergy(dynamicStore)` evaluates the `AllowMultiSign == 0` branch and returns `getExpireTimeForBandwidth() == 0`, which is `<= now`, so the "It's not time to unfreeze" check is bypassed.
5. Account A successfully reclaims the frozen TRX for ENERGY delegation before the actual expire time, while Account B's `AcquiredDelegatedFrozenBalanceForEnergy` bookkeeping is inconsistent with the premature unfreeze.

### Citations

**File:** chainbase/src/main/java/org/tron/core/capsule/DelegatedResourceCapsule.java (L119-125)
```java
  public long getExpireTimeForEnergy(DynamicPropertiesStore dynamicPropertiesStore) {
    if (dynamicPropertiesStore.getAllowMultiSign() == 0) {
      return this.delegatedResource.getExpireTimeForBandwidth();
    } else {
      return this.delegatedResource.getExpireTimeForEnergy();
    }
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/UnfreezeBalanceActuator.java (L400-429)
```java
        case ENERGY:
          if (delegatedResourceCapsule.getFrozenBalanceForEnergy() <= 0) {
            throw new ContractValidateException("no delegateFrozenBalance(Energy)");
          }
          if (dynamicStore.getAllowTvmConstantinople() == 0) {
            if (receiverCapsule.getAcquiredDelegatedFrozenBalanceForEnergy()
                < delegatedResourceCapsule.getFrozenBalanceForEnergy()) {
              throw new ContractValidateException(
                  "AcquiredDelegatedFrozenBalanceForEnergy[" + receiverCapsule
                      .getAcquiredDelegatedFrozenBalanceForEnergy() + "] < delegatedEnergy["
                      + delegatedResourceCapsule.getFrozenBalanceForEnergy() +
                      "]");
            }
          } else {
            if (dynamicStore.getAllowTvmSolidity059() != 1
                && receiverCapsule != null
                && receiverCapsule.getType() != AccountType.Contract
                && receiverCapsule.getAcquiredDelegatedFrozenBalanceForEnergy()
                < delegatedResourceCapsule.getFrozenBalanceForEnergy()) {
              throw new ContractValidateException(
                  "AcquiredDelegatedFrozenBalanceForEnergy[" + receiverCapsule
                      .getAcquiredDelegatedFrozenBalanceForEnergy() + "] < delegatedEnergy["
                      + delegatedResourceCapsule.getFrozenBalanceForEnergy() +
                      "]");
            }
          }

          if (delegatedResourceCapsule.getExpireTimeForEnergy(dynamicStore) > now) {
            throw new ContractValidateException("It's not time to unfreeze.");
          }
```
