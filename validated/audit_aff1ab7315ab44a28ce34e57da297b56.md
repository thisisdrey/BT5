This confirms the analog. In `FreezeBalanceActuator.execute()` and `FreezeBalanceProcessor.execute()`, when an owner freezes additional TRX for BANDWIDTH/ENERGY/TRON_POWER resources, the new `expireTime` is computed as `now + duration` and unconditionally overwrites the previous `expireTime` for the *entire* accumulated frozen balance via `AccountCapsule.setFrozenForBandwidth()` / `setFrozenForEnergy()` / `setFrozenForTronPower()`, which replace the single `Frozen` entry (`chainbase/.../AccountCapsule.java:1024-1041`). Since `UnfreezeBalanceActuator` only allows unfreezing once `now >= expireTime` for the whole frozen entry, any top-up freeze silently resets the unlock clock for previously-freezable TRX, exactly mirroring the reported bond-vesting-reset bug class. [1](#0-0) [2](#0-1) [3](#0-2) 

### Title
Freezing additional TRX resets the unfreeze lock time of the entire already-frozen balance - (File: actuator/src/main/java/org/tron/core/actuator/FreezeBalanceActuator.java)

### Summary
`FreezeBalanceContract` (legacy V1 freeze, still active for BANDWIDTH/ENERGY/TRON_POWER acquisition) and its TVM-callable equivalent `freeze()` (via `FreezeBalanceProcessor`) let an account owner top-up their frozen resource balance at any time. Each call recomputes `expireTime = now + frozenDuration * FROZEN_PERIOD` and stores it as the single expire time covering the account's *whole* accumulated frozen balance for that resource, not just the newly added amount.

### Finding Description
In `FreezeBalanceActuator.execute()`, for the BANDWIDTH case (no delegation), the code does:
```
long newFrozenBalanceForBandwidth = frozenBalance + accountCapsule.getFrozenBalance();
accountCapsule.setFrozenForBandwidth(newFrozenBalanceForBandwidth, expireTime);
```
`setFrozenForBandwidth()` in `AccountCapsule.java` replaces the single `Frozen` protobuf entry (index 0) that stores both the total `frozenBalance` and `expireTime` for the whole balance — there is no per-deposit tracking. The same pattern is repeated for ENERGY and TRON_POWER in this actuator, and identically in `FreezeBalanceProcessor.execute()` used by the TVM `freeze()` opcode (`Program.freeze()`).

Consequently, if a user already has TRX frozen and eligible to unfreeze (i.e. `now >= expireTime`), and then calls freeze again to add more TRX (even a small amount), the entire frozen balance's `expireTime` is overwritten with a new, later value (`now + duration`). This resets the lock/vesting period for funds that were previously already unlockable, forcing the user to wait the full freeze duration again before they can call `UnfreezeBalanceContract` to reclaim any of it — mirroring the reported `VaderBond.deposit()` bug class where a new deposit silently resets an existing claim/vesting clock.

### Impact Explanation
This causes unintended, permanent (until re-elapsed) freezing/locking of funds a user already had a right to withdraw. It can be triggered unintentionally by any wallet/dApp that adds bandwidth/energy resources incrementally (a common UX pattern), or triggered maliciously by a third party who has the owner's address details is not applicable here since it requires the owner's own signature — but it still creates unexpected temporary loss of access to funds for the legitimate owner, and could be exploited by protocols/bots that batch or auto top-up freezing to grief users' expected unlock schedules. This is a fund-locking / DoS-of-withdrawal issue rather than direct theft.

### Likelihood Explanation
Likelihood is high given normal user behavior: incremental freezing (freezing more TRX for bandwidth/energy while already having an active freeze) is an ordinary, expected operation and is explicitly supported by the actuator's accumulation logic (`frozenBalance + accountCapsule.getFrozenBalance()`), meaning virtually every "top-up freeze" transaction after the original lock period has partially or fully elapsed will silently reset the unlock time.

### Recommendation
Track the expire time per contribution instead of overwriting the singular whole-balance `expireTime`, or, when topping up, only extend the expire time if the new expire time is farther out than necessary, and otherwise preserve the earliest amount's unlock schedule (e.g., keep the minimum of old and new expire time for already-frozen principal, and only apply the new expiry to the incremental amount). At minimum, document this behavior explicitly and consider forcing/prompting an automatic unfreeze of currently-unlockable balance before applying the new freeze, analogous to the recommended mitigation from the referenced report (force redemption of claimable/unlockable funds before resetting the lock).

### Proof of Concept
1. Account A calls `FreezeBalanceContract` with `frozenDuration=3` days for BANDWIDTH → `expireTime = T0 + 3 days`.
2. Time passes; current time `now = T0 + 3 days + 1` (past expire, A is now eligible to `UnfreezeBalanceContract`).
3. Before calling unfreeze, A (or an automated wallet flow) calls `FreezeBalanceContract` again to add more bandwidth resource with `frozenDuration=3` days.
4. `FreezeBalanceActuator.execute()` sets `newFrozenBalanceForBandwidth = frozenBalance + oldFrozenBalance` and `expireTime = now + 3 days`, overwriting the account's single `Frozen` entry.
5. A's entire frozen balance (old + new) is now locked until the new `expireTime`, even though the old portion had already matured and was withdrawable moments before.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/FreezeBalanceActuator.java (L69-95)
```java
    long now = dynamicStore.getLatestBlockHeaderTimestamp();
    long duration = freezeBalanceContract.getFrozenDuration() * FROZEN_PERIOD;

    long newBalance = accountCapsule.getBalance() - freezeBalanceContract.getFrozenBalance();

    long frozenBalance = freezeBalanceContract.getFrozenBalance();
    long expireTime = now + duration;
    byte[] ownerAddress = freezeBalanceContract.getOwnerAddress().toByteArray();
    byte[] receiverAddress = freezeBalanceContract.getReceiverAddress().toByteArray();

    long increment;
    switch (freezeBalanceContract.getResource()) {
      case BANDWIDTH:
        if (!ArrayUtils.isEmpty(receiverAddress)
            && dynamicStore.supportDR()) {
          increment = delegateResource(ownerAddress, receiverAddress, true,
                  frozenBalance, expireTime);
          accountCapsule.addDelegatedFrozenBalanceForBandwidth(frozenBalance);
        } else {
          long oldNetWeight = accountCapsule.getFrozenBalance() / TRX_PRECISION;
          long newFrozenBalanceForBandwidth =
              frozenBalance + accountCapsule.getFrozenBalance();
          accountCapsule.setFrozenForBandwidth(newFrozenBalanceForBandwidth, expireTime);
          long newNetWeight = accountCapsule.getFrozenBalance() / TRX_PRECISION;
          increment = newNetWeight - oldNetWeight;
        }
        addTotalWeight(BANDWIDTH, dynamicStore, frozenBalance, increment);
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

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/FreezeBalanceProcessor.java (L73-116)
```java
  public void execute(FreezeBalanceParam param,  Repository repo) {
    // calculate expire time
    DynamicPropertiesStore dynamicStore = repo.getDynamicPropertiesStore();
    long nowInMs = dynamicStore.getLatestBlockHeaderTimestamp();
    long expireTime = nowInMs + param.getFrozenDuration() * FROZEN_PERIOD;

    byte[] ownerAddress = param.getOwnerAddress();
    byte[] receiverAddress = param.getReceiverAddress();
    long frozenBalance = param.getFrozenBalance();
    AccountCapsule accountCapsule = repo.getAccount(ownerAddress);
    // acquire or delegate resource
    if (param.isDelegating()) { // delegate resource
      switch (param.getResourceType()) {
        case BANDWIDTH:
          delegateResource(ownerAddress, receiverAddress,
              frozenBalance, expireTime, true, repo);
          accountCapsule.addDelegatedFrozenBalanceForBandwidth(frozenBalance);
          break;
        case ENERGY:
          delegateResource(ownerAddress, receiverAddress,
              frozenBalance, expireTime, false, repo);
          accountCapsule.addDelegatedFrozenBalanceForEnergy(frozenBalance);
          break;
        default:
          logger.debug("Resource Code Error.");
      }
    } else { // acquire resource
      switch (param.getResourceType()) {
        case BANDWIDTH:
          accountCapsule.setFrozenForBandwidth(
              frozenBalance + accountCapsule.getFrozenBalance(),
              expireTime);
          break;
        case ENERGY:
          accountCapsule.setFrozenForEnergy(
              frozenBalance + accountCapsule.getAccountResource()
                  .getFrozenBalanceForEnergy()
                  .getFrozenBalance(),
              expireTime);
          break;
        default:
          logger.debug("Resource Code Error.");
      }
    }
```
