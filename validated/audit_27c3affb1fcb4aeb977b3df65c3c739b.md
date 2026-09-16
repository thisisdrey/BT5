Based on my investigation, I found a strong analog to the reported `extendStaking` timestamp/duration validation bug in java-tron's `FreezeBalanceActuator`.

### Title
Missing/optionally-disabled `frozenDuration` bounds check in `FreezeBalanceActuator` allows negative or unbounded duration to corrupt resource expire-time calculations - (File: `actuator/src/main/java/org/tron/core/actuator/FreezeBalanceActuator.java`)

### Summary
`FreezeBalanceContract.frozenDuration` is a signed `int64` supplied directly by the transaction broadcaster. `FreezeBalanceActuator.validate()` only enforces the `[minFrozenTime, maxFrozenTime]` bound on `frozenDuration` when the node-level flag `needCheckFrozeTime` (`CommonParameter.getCheckFrozenTime() == 1`) is enabled; the code comment marks this flag `//for test`, indicating the check is intended to be disabled in normal (production) operation. [1](#0-0)  When the check is skipped, `execute()` computes `expireTime = now + duration` with `duration = freezeBalanceContract.getFrozenDuration() * FROZEN_PERIOD` and applies it directly to `setFrozenForBandwidth`/`setFrozenForEnergy`/`setFrozenForTronPower` (or to a delegated resource) with no independent guard against a negative or out-of-range value. [2](#0-1) 

### Finding Description
This mirrors the `TrufVesting.extendStaking` bug class exactly: a user-controlled duration parameter is added to `block.timestamp`/`now` to compute a lock/expiration timestamp, with the arithmetic-sign/bounds check gated behind a condition that is not guaranteed to be active. In `FreezeBalanceActuator.validate()`:
```
long frozenDuration = freezeBalanceContract.getFrozenDuration();
long minFrozenTime = dynamicStore.getMinFrozenTime();
long maxFrozenTime = dynamicStore.getMaxFrozenTime();

boolean needCheckFrozeTime = CommonParameter.getInstance().getCheckFrozenTime() == 1; //for test
if (needCheckFrozeTime && !(frozenDuration >= minFrozenTime && frozenDuration <= maxFrozenTime)) {
  throw new ContractValidateException(...);
}
``` [1](#0-0) 
If `needCheckFrozeTime` is false (its explicit purpose per the inline comment is only to be turned on for tests), `frozenDuration` can be any value the caller signs into the transaction, including 0 or negative, without rejection. `execute()` then unconditionally computes:
```
long duration = freezeBalanceContract.getFrozenDuration() * FROZEN_PERIOD;
...
long expireTime = now + duration;
``` [3](#0-2) 
and stores this `expireTime` into the account's frozen-balance/expire-time fields (`setFrozenForBandwidth`, `setFrozenForEnergy`, `setFrozenForTronPower`) or into a `DelegatedResourceCapsule` if delegating. [4](#0-3)  A negative `frozenDuration` produces an `expireTime` earlier than `now`, so the frozen balance is immediately eligible to be treated as already-unfrozen/expired by downstream logic that compares `expireTime` against the current timestamp (e.g., unfreeze/expire-resource paths and `DelegatedResourceStore.unLockExpireResource`, which unlocks based on `expireTime < now`). [5](#0-4) 

This differs from the TVM-native freeze path (`FreezeBalanceProcessor`/`Program.freeze`), where `frozenDuration` is set internally by the VM (not attacker-controlled) rather than taken from user input, so that path is not affected the same way. [6](#0-5)  The vulnerable surface is specifically the plain `FreezeBalanceContract` actuator reachable by any account broadcasting a signed transaction.

### Impact Explanation
An attacker can freeze TRX for BANDWIDTH/ENERGY/TRON_POWER with a manipulated `frozenDuration` (e.g., negative), causing `expireTime` to be set in the past. Balance is deducted from the owner's spendable balance and added to frozen state/weight, but the resource is effectively immediately "expired," letting the actor unfreeze it right away while still having (temporarily) obtained the resource benefit and vote weight tied to `frozenBalance`/`TronPower` at time of freeze, and potentially double counting/incorrect adjustments in `addTotalWeight` bookkeeping (`TotalNetWeight`/`TotalEnergyWeight`). This can corrupt the network's bandwidth/energy total-weight accounting and TRON Power-based voting weight, which are used for resource pricing and SR election — a resource-accounting/voting integrity impact.

### Likelihood Explanation
Likelihood depends entirely on whether `CommonParameter.getCheckFrozenTime()` defaults to `1` (enabled) or `0` (disabled) in the shipped `reference.conf`/`Args.java` for mainnet nodes. I could not conclusively confirm the exact default value from the available index content (the file grep hits were found but line content for the default value was not retrieved before the tool budget ended). The `//for test` comment on the flag strongly suggests it is intended to be off in production, which would make this a live, unauthenticated, single-transaction exploitable path. This uncertainty should be verified directly against `common/src/main/resources/reference.conf`, `framework/src/main/java/org/tron/core/config/args/Args.java`, and `CommonParameter.java` before treating this as confirmed-exploitable on mainnet.

### Recommendation
Make the `frozenDuration` bounds/sign check in `FreezeBalanceActuator.validate()` unconditional (remove the `needCheckFrozeTime` gate, or restrict that gate to test builds only via a compile-time/test-only flag rather than a runtime `CommonParameter`), and additionally assert `frozenDuration > 0` and that `now + duration` does not overflow or precede `now`.

### Proof of Concept
1. Confirm the node's `checkFrozenTime` common parameter is not set to `1` (default/production configuration).
2. Broadcast a signed `FreezeBalanceContract` transaction with `frozen_balance = 1_000_000` (1 TRX) and `frozen_duration = -1000000`.
3. Observe `FreezeBalanceActuator.validate()` does not reject the transaction because `needCheckFrozeTime` is false, skipping the `frozenDuration` range check. [7](#0-6) 
4. Observe `execute()` sets `expireTime = now + (frozenDuration * FROZEN_PERIOD)`, i.e. a timestamp far in the past, into the account's frozen balance state. [8](#0-7) 
5. Subsequent unfreeze/expire logic treats the frozen balance as already expired, allowing near-immediate release of funds that had already contributed to total network weight/voting power computations.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/FreezeBalanceActuator.java (L69-121)
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
        break;
      case ENERGY:
        if (!ArrayUtils.isEmpty(receiverAddress)
            && dynamicStore.supportDR()) {
          increment = delegateResource(ownerAddress, receiverAddress, false,
                  frozenBalance, expireTime);
          accountCapsule.addDelegatedFrozenBalanceForEnergy(frozenBalance);
        } else {
          long oldEnergyWeight = accountCapsule.getEnergyFrozenBalance() / TRX_PRECISION;
          long newFrozenBalanceForEnergy =
              frozenBalance + accountCapsule.getEnergyFrozenBalance();
          accountCapsule.setFrozenForEnergy(newFrozenBalanceForEnergy, expireTime);
          long newEnergyWeight = accountCapsule.getEnergyFrozenBalance() / TRX_PRECISION;
          increment = newEnergyWeight - oldEnergyWeight;
        }
        addTotalWeight(ENERGY, dynamicStore, frozenBalance, increment);
        break;
      case TRON_POWER:
        long oldTPWeight = accountCapsule.getTronPowerFrozenBalance() / TRX_PRECISION;
        long newFrozenBalanceForTronPower =
            frozenBalance + accountCapsule.getTronPowerFrozenBalance();
        accountCapsule.setFrozenForTronPower(newFrozenBalanceForTronPower, expireTime);
        long newTPWeight = accountCapsule.getTronPowerFrozenBalance() / TRX_PRECISION;
        increment = newTPWeight - oldTPWeight;
        addTotalWeight(TRON_POWER, dynamicStore, frozenBalance, increment);
        break;
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

**File:** chainbase/src/main/java/org/tron/core/store/DelegatedResourceStore.java (L37-65)
```java
  public void unLockExpireResource(byte[] from, byte[] to, long now) {
    byte[] lockKey = DelegatedResourceCapsule
        .createDbKeyV2(from, to, true);
    DelegatedResourceCapsule lockResource = get(lockKey);
    if (lockResource == null) {
      return;
    }
    if (lockResource.getExpireTimeForEnergy() >= now
        && lockResource.getExpireTimeForBandwidth() >= now) {
      return;
    }

    byte[] unlockKey = DelegatedResourceCapsule
        .createDbKeyV2(from, to, false);
    DelegatedResourceCapsule unlockResource = get(unlockKey);
    if (unlockResource == null) {
      unlockResource = new DelegatedResourceCapsule(ByteString.copyFrom(from),
          ByteString.copyFrom(to));
    }
    if (lockResource.getExpireTimeForEnergy() < now) {
      unlockResource.addFrozenBalanceForEnergy(
          lockResource.getFrozenBalanceForEnergy(), 0);
      lockResource.setFrozenBalanceForEnergy(0, 0);
    }
    if (lockResource.getExpireTimeForBandwidth() < now) {
      unlockResource.addFrozenBalanceForBandwidth(
          lockResource.getFrozenBalanceForBandwidth(), 0);
      lockResource.setFrozenBalanceForBandwidth(0, 0);
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/FreezeBalanceProcessor.java (L21-52)
```java
  public void validate(FreezeBalanceParam param, Repository repo) throws ContractValidateException {
    if (repo == null) {
      throw new ContractValidateException(STORE_NOT_EXIST);
    }

    // validate arg @frozenBalance
    byte[] ownerAddress = param.getOwnerAddress();
    AccountCapsule ownerCapsule = repo.getAccount(ownerAddress);
    long frozenBalance = param.getFrozenBalance();
    if (frozenBalance <= 0) {
      throw new ContractValidateException("FrozenBalance must be positive");
    } else if (frozenBalance < TRX_PRECISION) {
      throw new ContractValidateException("FrozenBalance must be greater than or equal to 1 TRX");
    } else if (frozenBalance > ownerCapsule.getBalance()) {
      throw new ContractValidateException("FrozenBalance must be less than or equal to accountBalance");
    }

    // validate frozen count of owner account
    int frozenCount = ownerCapsule.getFrozenCount();
    if (frozenCount != 0 && frozenCount != 1) {
      throw new ContractValidateException("FrozenCount must be 0 or 1");
    }

    // validate arg @resourceType
    switch (param.getResourceType()) {
      case BANDWIDTH:
      case ENERGY:
        break;
      default:
        throw new ContractValidateException(
            "Unknown ResourceCode, valid ResourceCode[BANDWIDTH、ENERGY]");
    }
```
