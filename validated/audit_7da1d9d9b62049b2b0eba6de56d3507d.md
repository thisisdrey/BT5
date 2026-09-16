Based on my investigation of the `UnDelegateResourceActuator.execute()` path, I found a genuine null-dereference bug analogous to the reported kernel issue: an object that can be absent/removed (like the freed `ifa` in the kernel case) is dereferenced later in the same method without a null-guard, on a path reachable by a single signed transaction.

### Title
NullPointerException / transaction execution abort in `UnDelegateResourceActuator.execute()` when the unlock `DelegatedResourceCapsule` is missing - ([File: actuator/src/main/java/org/tron/core/actuator/UnDelegateResourceActuator.java])

### Summary
`UnDelegateResourceActuator.execute()` fetches the unlock `DelegatedResourceCapsule` via `delegatedResourceStore.get(unlockKey)` and then unconditionally dereferences it (`unlockResource.addFrozenBalanceForBandwidth(...)` / `addFrozenBalanceForEnergy(...)`) without checking for `null`, even though `validate()` only guarantees that *either* the unlock or the lock resource exists, not that the unlock one specifically does.

### Finding Description
In `execute()`: [1](#0-0) 
`unlockResource` is fetched from `delegatedResourceStore.get(unlockKey)` and can legitimately be `null` (e.g., when only a locked delegation exists and no unlocked one has ever been created). The subsequent `switch` block calls `unlockResource.addFrozenBalanceForBandwidth(-unDelegateBalance, 0)` or `unlockResource.addFrozenBalanceForEnergy(-unDelegateBalance, 0)` directly on this potentially-null reference.

Compare with `validate()`: [2](#0-1) 
Validation only requires that at least one of `unlockResourceCapsule` or `lockResourceCapsule` (locked and expired) is non-null and that their combined `delegateBalance` covers `unDelegateBalance`. This means a transaction can pass `validate()` purely based on an expired *locked* delegation while the *unlocked* resource entry (`unlockKey`) does not exist at all, exactly mirroring the kernel bug's root cause: an object accessed after a point where it could already be gone, with the check for "is it gone" happening too late (or, here, not at all before the dereference).

This is directly reachable by any account holder that has ever delegated bandwidth/energy with a lock period, then calls `UnDelegateResourceContract` before ever having created an unlocked delegation record for the same resource type, or after the unlocked record was deleted (deletion happens right in this same method at line 172: `delegatedResourceStore.delete(unlockKey); unlockResource = null;`, but that's after use in the same call so it's fine for repeat calls, not the missing-on-first-call case). The unauthorized-analog case is: delegate only with `lock=true`, wait past expiry, then immediately call `UnDelegateResourceContract` targeting bandwidth/energy for a balance that is only satisfied by the locked-and-expired amount — `unlockResourceCapsule` is `null` at validation and remains `null` at execution, and `execute()` dereferences it unconditionally.

### Impact Explanation
An unprivileged transaction broadcaster can craft a transaction that passes `validate()` but throws an unhandled `NullPointerException` inside `execute()`. Depending on how the block-application code in `Manager`/`TransactionTrace` handles unchecked runtime exceptions bubbling out of an actuator's `execute()` (as opposed to the declared `ContractExeException`/`ContractValidateException`), this can manifest as an unexpected exception path during block processing — a potential node crash/halt or transaction-processing inconsistency, since actuator interfaces are only declared to throw checked exceptions and callers may not defensively catch `RuntimeException` at every layer.

### Likelihood Explanation
High likelihood of reachability: delegate-with-lock is a normal, unprivileged operation (`DelegateResourceContract` with `lock=true`), and undelegating after lock expiry without ever creating an unlocked delegation entry is a completely ordinary sequence a user could hit unintentionally or trigger intentionally to test node behavior.

### Recommendation
Add a null check for `unlockResource` in `execute()` mirroring the `validate()` logic — if `unlockResource` is `null`, create a new `DelegatedResourceCapsule` (as already done for the `delegatedResourceCapsule == null` case in `DelegateResourceActuator.delegateResource()`, see [3](#0-2) ) or otherwise handle the case explicitly before calling `addFrozenBalanceForBandwidth`/`addFrozenBalanceForEnergy`, so that unlocked balance drawn purely from the expired-lock portion doesn't NPE.

### Proof of Concept
1. Account A freezes and delegates a resource (BANDWIDTH or ENERGY) to account B with `lock=true` and a short lock period (`DelegateResourceContract`), never delegating any amount with `lock=false` to B.
2. Wait until the lock period expires (`expireTime < now`), satisfying `validate()`'s check via `lockResourceCapsule.getExpireTimeForBandwidth() < now` branch.
3. Broadcast `UnDelegateResourceContract` from A to B for the resource, with `unDelegateBalance` equal to the locked amount.
4. `validate()` passes (uses `lockResourceCapsule`'s expired balance). `execute()` computes `unlockKey` (`lock=false`) and calls `delegatedResourceStore.get(unlockKey)`, which returns `null` since no unlocked delegation was ever created; the subsequent `unlockResource.addFrozenBalanceForBandwidth(...)` throws `NullPointerException`. [4](#0-3)

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/UnDelegateResourceActuator.java (L128-176)
```java
    byte[] unlockKey = DelegatedResourceCapsule
        .createDbKeyV2(ownerAddress, receiverAddress, false);
    DelegatedResourceCapsule unlockResource = delegatedResourceStore
        .get(unlockKey);

    // modify owner Account
    AccountCapsule ownerCapsule = accountStore.get(ownerAddress);
    switch (unDelegateResourceContract.getResource()) {
      case BANDWIDTH: {
        unlockResource.addFrozenBalanceForBandwidth(-unDelegateBalance, 0);

        ownerCapsule.addDelegatedFrozenV2BalanceForBandwidth(-unDelegateBalance);
        ownerCapsule.addFrozenBalanceForBandwidthV2(unDelegateBalance);

        BandwidthProcessor processor = new BandwidthProcessor(chainBaseManager);

        long now = chainBaseManager.getHeadSlot();
        if (Objects.nonNull(receiverCapsule) && transferUsage > 0) {
          processor.unDelegateIncrease(ownerCapsule, receiverCapsule,
              transferUsage, BANDWIDTH, now);
        }
      }
      break;
      case ENERGY: {
        unlockResource.addFrozenBalanceForEnergy(-unDelegateBalance, 0);

        ownerCapsule.addDelegatedFrozenV2BalanceForEnergy(-unDelegateBalance);
        ownerCapsule.addFrozenBalanceForEnergyV2(unDelegateBalance);

        EnergyProcessor processor = new EnergyProcessor(dynamicStore, accountStore);

        long now = chainBaseManager.getHeadSlot();
        if (Objects.nonNull(receiverCapsule) && transferUsage > 0) {
          processor.unDelegateIncrease(ownerCapsule, receiverCapsule, transferUsage, ENERGY, now);
        }
      }
      break;
      default:
        //this should never happen
        break;
    }

    if (unlockResource.getFrozenBalanceForBandwidth() == 0
        && unlockResource.getFrozenBalanceForEnergy() == 0) {
      delegatedResourceStore.delete(unlockKey);
      unlockResource = null;
    } else {
      delegatedResourceStore.put(unlockKey, unlockResource);
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/UnDelegateResourceActuator.java (L256-304)
```java
    byte[] key = DelegatedResourceCapsule.createDbKeyV2(ownerAddress, receiverAddress, false);
    DelegatedResourceCapsule unlockResourceCapsule = delegatedResourceStore.get(key);
    byte[] lockKey = DelegatedResourceCapsule.createDbKeyV2(ownerAddress, receiverAddress, true);
    DelegatedResourceCapsule lockResourceCapsule = delegatedResourceStore.get(lockKey);
    if (unlockResourceCapsule == null && lockResourceCapsule == null) {
      throw new ContractValidateException(
          "delegated Resource does not exist");
    }

    long unDelegateBalance = unDelegateResourceContract.getBalance();
    if (unDelegateBalance <= 0) {
      throw new ContractValidateException("unDelegateBalance must be more than 0 TRX");
    }
    switch (unDelegateResourceContract.getResource()) {
      case BANDWIDTH: {
        long delegateBalance = 0;
        if (unlockResourceCapsule != null) {
          delegateBalance += unlockResourceCapsule.getFrozenBalanceForBandwidth();
        }
        if (lockResourceCapsule != null
            && lockResourceCapsule.getExpireTimeForBandwidth() < now) {
          delegateBalance += lockResourceCapsule.getFrozenBalanceForBandwidth();
        }
        if (delegateBalance < unDelegateBalance) {
          throw new ContractValidateException(
              "insufficient delegatedFrozenBalance(BANDWIDTH), request="
                  + unDelegateBalance + ", unlock_balance=" + delegateBalance);
        }
      }
      break;
      case ENERGY: {
        long delegateBalance = 0;
        if (unlockResourceCapsule != null) {
          delegateBalance += unlockResourceCapsule.getFrozenBalanceForEnergy();
        }
        if (lockResourceCapsule != null
            && lockResourceCapsule.getExpireTimeForEnergy() < now) {
          delegateBalance += lockResourceCapsule.getFrozenBalanceForEnergy();
        }
        if (delegateBalance < unDelegateBalance) {
          throw new ContractValidateException("insufficient delegateFrozenBalance(Energy), request="
              + unDelegateBalance + ", unlock_balance=" + delegateBalance);
        }
      }
      break;
      default:
        throw new ContractValidateException(
            "ResourceCode error.valid ResourceCode[BANDWIDTH、Energy]");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/DelegateResourceActuator.java (L299-304)
```java
    byte[] key = DelegatedResourceCapsule.createDbKeyV2(ownerAddress, receiverAddress, lock);
    DelegatedResourceCapsule delegatedResourceCapsule = delegatedResourceStore.get(key);
    if (delegatedResourceCapsule == null) {
      delegatedResourceCapsule = new DelegatedResourceCapsule(ByteString.copyFrom(ownerAddress),
          ByteString.copyFrom(receiverAddress));
    }
```
