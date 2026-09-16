## Title
FreezeBalanceActuator overwrites the shared delegated-resource lock expiry, allowing early release of previously-committed frozen TRX - (File: `actuator/src/main/java/org/tron/core/actuator/FreezeBalanceActuator.java`)

## Summary
The legacy `FreezeBalanceContract` flow stores a single `expire_time_for_bandwidth`/`expire_time_for_energy` field per `(owner, receiver)` pair in `DelegatedResourceCapsule`, shared across *all* delegations ever made to that pair, exactly like the CrossPool flaw of reusing one "end investment block & time lock" for every deposit into a pool rather than tracking per-deposit expiry. Each new `FreezeBalanceContract` delegation to the same receiver unconditionally overwrites that shared expiry with the new, independently-chosen duration, with no check against the remaining lock time of previously delegated (and still locked) balance.

## Finding Description
`FreezeBalanceActuator.delegateResource()` accumulates the frozen balance and blindly overwrites the expiry time on every call: [1](#0-0) 

`DelegatedResourceCapsule.addFrozenBalanceForBandwidth`/`addFrozenBalanceForEnergy` sum the balance but always set `expireTime` to whatever value is passed in, with no `max(...)` guard: [2](#0-1) 

`FreezeBalanceActuator.validate()` only checks that `frozenDuration` is within `[minFrozenTime, maxFrozenTime]` for the *new* delegation; it never inspects the existing `DelegatedResourceCapsule` for the same `(owner, receiver)` to ensure the new expiry cannot be earlier than an already-locked expiry: [3](#0-2) 

This is in sharp contrast to the newer `DelegateResourceActuator` (v2 delegate/lock flow), which explicitly enforces that a new lock period cannot shorten the remaining time of the last lock period for the same resource: [4](#0-3) 

Finally, `UnfreezeBalanceActuator.validate()` gates the unlock solely on the single, shared expiry field: [5](#0-4) [6](#0-5) 

Because the expiry is per-pair rather than per-delegation, an owner can extend the pooled balance while shrinking the effective lock: freeze a large amount for the maximum lock duration to a receiver, then immediately freeze a small additional amount to the same receiver with the minimum allowed duration. The second call overwrites `expireTimeForBandwidth`/`expireTimeForEnergy` to the short value while `frozenBalanceFor*` becomes the sum of both amounts. Once the short period elapses, `UnfreezeBalanceActuator` allows the owner to withdraw the *entire* pooled amount — including the portion that was supposed to remain locked for the long duration.

## Impact Explanation
This breaks the resource-delegation lock invariant that other participants (e.g., energy/bandwidth rental services, receivers relying on delegated resources being available for a committed duration) depend on. An owner can unilaterally shorten a previously committed lock period on delegated TRX and reclaim funds earlier than agreed, and the receiver's granted resource weight disappears earlier than expected as well — directly analogous to CrossPool's "shared end block" issue where per-deposit lock guarantees are silently overridden by a later operation using the shared field.

## Likelihood Explanation
Reachable via two ordinary signed `FreezeBalanceContract` transactions from the same account to the same receiver — no special privileges, no witness/SR role, and no contract deployment required. It is fully deterministic and reproducible on any chain where legacy freeze (pre-`supportUnfreezeDelay`/freeze-v2) is still active.

## Recommendation
When delegating additional balance to an existing `(owner, receiver)` pair, do not overwrite the expiry time unconditionally. Track expiry per lock period (as `DelegateResourceActuator`'s v2 lock model with `validRemainTime` does), or at minimum take `max(existingExpireTime, newExpireTime)` when merging balances in `DelegatedResourceCapsule.addFrozenBalanceForBandwidth`/`addFrozenBalanceForEnergy`, and add a `FreezeBalanceActuator.validate()` check preventing a new delegation's duration from being shorter than the remaining lock time of the existing delegated resource to the same receiver.

## Proof of Concept
1. Account A freezes `X` TRX for BANDWIDTH, delegating to receiver B, with `frozenDuration = maxFrozenTime` (long lock). `DelegatedResourceCapsule(A,B).frozenBalanceForBandwidth = X`, `expireTimeForBandwidth = now + maxFrozenTime`.
2. Immediately after, Account A freezes `1 TRX` for BANDWIDTH, delegating to the same receiver B, with `frozenDuration = minFrozenTime` (short lock). `delegateResource()` sums balances (`frozenBalanceForBandwidth = X + 1`) and overwrites `expireTimeForBandwidth = now + minFrozenTime`.
3. After `minFrozenTime` elapses (but well before `maxFrozenTime`), Account A calls `UnfreezeBalanceContract` for B. `UnfreezeBalanceActuator.validate()` passes because `expireTimeForBandwidth <= now`, and `execute()` releases the full `X + 1` TRX back to A's balance, despite `X` having been committed for `maxFrozenTime`.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/FreezeBalanceActuator.java (L203-214)
```java
    long frozenDuration = freezeBalanceContract.getFrozenDuration();
    long minFrozenTime = dynamicStore.getMinFrozenTime();
    long maxFrozenTime = dynamicStore.getMaxFrozenTime();

    boolean needCheckFrozeTime = CommonParameter.getInstance()
        .getCheckFrozenTime() == 1;//for test
    if (needCheckFrozeTime && !(frozenDuration >= minFrozenTime
        && frozenDuration <= maxFrozenTime)) {
      throw new ContractValidateException(
          "frozenDuration must be less than " + maxFrozenTime + " days "
              + "and more than " + minFrozenTime + " days");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/FreezeBalanceActuator.java (L296-317)
```java
    byte[] key = DelegatedResourceCapsule.createDbKey(ownerAddress, receiverAddress);
    //modify DelegatedResourceStore
    DelegatedResourceCapsule delegatedResourceCapsule = delegatedResourceStore
        .get(key);
    if (delegatedResourceCapsule != null) {
      if (isBandwidth) {
        delegatedResourceCapsule.addFrozenBalanceForBandwidth(balance, expireTime);
      } else {
        delegatedResourceCapsule.addFrozenBalanceForEnergy(balance, expireTime);
      }
    } else {
      delegatedResourceCapsule = new DelegatedResourceCapsule(
          ByteString.copyFrom(ownerAddress),
          ByteString.copyFrom(receiverAddress));
      if (isBandwidth) {
        delegatedResourceCapsule.setFrozenBalanceForBandwidth(balance, expireTime);
      } else {
        delegatedResourceCapsule.setFrozenBalanceForEnergy(balance, expireTime);
      }

    }
    delegatedResourceStore.put(key, delegatedResourceCapsule);
```

**File:** chainbase/src/main/java/org/tron/core/capsule/DelegatedResourceCapsule.java (L70-103)
```java
  public void addFrozenBalanceForEnergy(long energy, long expireTime) {
    this.delegatedResource = this.delegatedResource.toBuilder()
        .setFrozenBalanceForEnergy(this.delegatedResource.getFrozenBalanceForEnergy() + energy)
        .setExpireTimeForEnergy(expireTime)
        .build();
  }

  public long getFrozenBalanceForBandwidth() {
    return this.delegatedResource.getFrozenBalanceForBandwidth();
  }

  public long getFrozenBalance(boolean isBandwidth) {
    if (isBandwidth) {
      return getFrozenBalanceForBandwidth();
    } else {
      return getFrozenBalanceForEnergy();
    }

  }

  public void setFrozenBalanceForBandwidth(long bandwidth, long expireTime) {
    this.delegatedResource = this.delegatedResource.toBuilder()
        .setFrozenBalanceForBandwidth(bandwidth)
        .setExpireTimeForBandwidth(expireTime)
        .build();
  }

  public void addFrozenBalanceForBandwidth(long bandwidth, long expireTime) {
    this.delegatedResource = this.delegatedResource.toBuilder()
        .setFrozenBalanceForBandwidth(this.delegatedResource.getFrozenBalanceForBandwidth()
            + bandwidth)
        .setExpireTimeForBandwidth(expireTime)
        .build();
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/DelegateResourceActuator.java (L261-270)
```java
  private void validRemainTime(ResourceCode resourceCode, long lockPeriod, long expireTime,
      long now) throws ContractValidateException {
    long remainTime = expireTime - now;
    if (lockPeriod * BLOCK_PRODUCED_INTERVAL < remainTime) {
      throw new ContractValidateException(
          "The lock period for " + resourceCode.name() + " this time cannot be less than the "
              + "remaining time[" + remainTime + "ms] of the last lock period for "
              + resourceCode.name() + "!");
    }
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/UnfreezeBalanceActuator.java (L396-398)
```java
          if (delegatedResourceCapsule.getExpireTimeForBandwidth() > now) {
            throw new ContractValidateException("It's not time to unfreeze.");
          }
```

**File:** actuator/src/main/java/org/tron/core/actuator/UnfreezeBalanceActuator.java (L427-429)
```java
          if (delegatedResourceCapsule.getExpireTimeForEnergy(dynamicStore) > now) {
            throw new ContractValidateException("It's not time to unfreeze.");
          }
```
