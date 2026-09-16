### Title
FreezeBalanceActuator resets lock expiry to a shorter duration when re-freezing (increasing) an already-locked balance, allowing users to unlock TRON Power / bandwidth-energy stake earlier than promised - (File: actuator/src/main/java/org/tron/core/actuator/FreezeBalanceActuator.java)

### Summary
`FreezeBalanceActuator` (the legacy Stake 1.0 `FreezeBalanceContract` handler) lets an account owner add more TRX to an existing frozen (locked) balance for BANDWIDTH, ENERGY, or TRON_POWER. When it does so it always recomputes `expireTime = now + frozenDuration * FROZEN_PERIOD` from the incoming transaction's `frozenDuration`, and overwrites the account's stored expire time with this new value, regardless of how much time was remaining on the previous freeze. There is no check that the newly supplied `frozenDuration` is at least as long as the remaining time of the currently active freeze.

### Finding Description
In `FreezeBalanceActuator.validate()` (`actuator/src/main/java/org/tron/core/actuator/FreezeBalanceActuator.java:203-214`) the only duration check performed is: [1](#0-0) 
this compares `frozenDuration` against the global `minFrozenTime`/`maxFrozenTime` bounds only — it never compares the new duration against the remaining time of any freeze the account already has active (i.e., `accountCapsule.getFrozenList().get(0).getExpireTime()` for bandwidth, or the equivalent for energy/TRON_POWER).

In `execute()`, when the owner freezes additional balance for a resource it already has frozen, the code adds the new amount to the existing frozen balance but simply overwrites the expire time with the new, independently-computed expiry: [2](#0-1) [3](#0-2) [4](#0-3) 
`setFrozenForBandwidth`/`setFrozenForEnergy`/`setFrozenForTronPower` simply replace the stored `Frozen.expire_time` with the caller-supplied value: [5](#0-4) 

This is exactly the same bug class as the Merit Circle `TimeLockPool.increaseLock()` finding: an entire pool of already-locked value is silently re-lockable for a shorter period than originally promised, because the "increase" path re-derives the expiry from the new call parameters instead of enforcing `newExpireTime >= previousExpireTime` (or equivalently `newDuration >= remainingDuration`).

By contrast, the newer Stake 2.0 lock mechanism, `DelegateResourceActuator`, explicitly guards against this: it computes the `remainTime` from the existing lock and rejects any re-lock whose duration would result in an earlier expiry than already committed: [6](#0-5) 
The absence of an equivalent `validRemainTime`-style check in `FreezeBalanceActuator` for the legacy (but still reachable, unauthenticated-by-anyone-who-has-the-account) Stake 1.0 path is the root cause.

### Impact Explanation
An account that has frozen TRX for BANDWIDTH, ENERGY, or TRON_POWER with a long duration (e.g., close to `maxFrozenTime`) can call `FreezeBalanceContract` again with only `minFrozenTime` (currently 3 days) and a trivial additional amount (as low as 1 TRX, satisfying `frozenBalance >= TRX_PRECISION`). The actuator will merge the balances but reset `expireTime` to `now + minFrozenTime`, shortening the lock on the entire pooled frozen balance — including the portion that was supposed to remain locked far longer. Any resource/vote weight derived from the frozen balance becomes unfreezable and the corresponding TRX withdrawable much earlier than the network/protocol intended, undermining resource/vote-weight economics tied to lock duration guarantees (bandwidth/energy weight, TRON Power for voting).

### Likelihood Explanation
Any account holder can trigger this themselves at will by broadcasting two `FreezeBalanceContract` transactions (first with a long duration, later a second with the minimum duration and minimal extra balance) — no privileged role or third party is required, and the actuator raises no error for this sequence. This is a Medium-severity, easily reproducible self-service issue for any user of the legacy freeze path.

### Recommendation
In `FreezeBalanceActuator.validate()`/`execute()`, when the account already holds a non-zero frozen balance for the target resource, require that the new `expireTime` (`now + frozenDuration * FROZEN_PERIOD`) not be earlier than the existing stored expire time (mirroring the `validRemainTime` check already used in `DelegateResourceActuator`), or take `expireTime = max(now + frozenDuration * FROZEN_PERIOD, existingExpireTime)` when merging balances.

### Proof of Concept
1. Account A calls `FreezeBalanceContract` with `frozen_balance = X`, `frozen_duration = maxFrozenTime` (e.g., 3 days by default config, but longer in some configs/testnets), resource = BANDWIDTH. `AccountCapsule` now stores `Frozen{frozenBalance=X, expireTime = now + maxFrozenTime*FROZEN_PERIOD}`.
2. Before that period elapses, Account A calls `FreezeBalanceContract` again with `frozen_balance = 1 (TRX_PRECISION)`, `frozen_duration = minFrozenTime` (3 days), same resource.
3. `validate()` passes because `frozenDuration` (3 days) is within `[minFrozenTime, maxFrozenTime]` — no comparison to the existing freeze's remaining time is made.
4. `execute()` merges balances: `newFrozenBalanceForBandwidth = 1 + X`, and calls `setFrozenForBandwidth(X+1, now + 3days)`, overwriting the previous longer expiry.
5. Account A can now unfreeze the entire `X+1` balance after only 3 days, despite having originally committed `X` for the longer period.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/FreezeBalanceActuator.java (L87-93)
```java
        } else {
          long oldNetWeight = accountCapsule.getFrozenBalance() / TRX_PRECISION;
          long newFrozenBalanceForBandwidth =
              frozenBalance + accountCapsule.getFrozenBalance();
          accountCapsule.setFrozenForBandwidth(newFrozenBalanceForBandwidth, expireTime);
          long newNetWeight = accountCapsule.getFrozenBalance() / TRX_PRECISION;
          increment = newNetWeight - oldNetWeight;
```

**File:** actuator/src/main/java/org/tron/core/actuator/FreezeBalanceActuator.java (L104-110)
```java
          long oldEnergyWeight = accountCapsule.getEnergyFrozenBalance() / TRX_PRECISION;
          long newFrozenBalanceForEnergy =
              frozenBalance + accountCapsule.getEnergyFrozenBalance();
          accountCapsule.setFrozenForEnergy(newFrozenBalanceForEnergy, expireTime);
          long newEnergyWeight = accountCapsule.getEnergyFrozenBalance() / TRX_PRECISION;
          increment = newEnergyWeight - oldEnergyWeight;
        }
```

**File:** actuator/src/main/java/org/tron/core/actuator/FreezeBalanceActuator.java (L114-119)
```java
        long oldTPWeight = accountCapsule.getTronPowerFrozenBalance() / TRX_PRECISION;
        long newFrozenBalanceForTronPower =
            frozenBalance + accountCapsule.getTronPowerFrozenBalance();
        accountCapsule.setFrozenForTronPower(newFrozenBalanceForTronPower, expireTime);
        long newTPWeight = accountCapsule.getTronPowerFrozenBalance() / TRX_PRECISION;
        increment = newTPWeight - oldTPWeight;
```

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

**File:** chainbase/src/main/java/org/tron/core/capsule/AccountCapsule.java (L1024-1041)
```java
  public void setFrozenForBandwidth(long frozenBalance, long expireTime) {
    Frozen newFrozen = Frozen.newBuilder()
        .setFrozenBalance(frozenBalance)
        .setExpireTime(expireTime)
        .build();

    long frozenCount = getFrozenCount();
    if (frozenCount == 0) {
      setInstance(getInstance().toBuilder()
          .addFrozen(newFrozen)
          .build());
    } else {
      setInstance(getInstance().toBuilder()
          .setFrozen(0, newFrozen)
          .build()
      );
    }
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
