### Title
Re-locking (freezing) additional balance before an existing freeze expires overwrites the expiration timestamp instead of preserving the original commitment - (File: `actuator/src/main/java/org/tron/core/actuator/FreezeBalanceActuator.java`, `actuator/src/main/java/org/tron/core/vm/nativecontract/FreezeBalanceProcessor.java`)

### Summary
`FreezeBalanceActuator.execute()` and its TVM-opcode analog `FreezeBalanceProcessor.execute()` compute a brand-new `expireTime = now + frozenDuration * FROZEN_PERIOD` every time a `FreezeBalanceContract`/`freeze()` opcode call succeeds, and then blindly overwrite the single stored `Frozen.expireTime` field for the resource (bandwidth/energy/TRON_POWER) with this new value via `AccountCapsule.setFrozenForBandwidth`/`setFrozenForEnergy`/`setFrozenForTronPower`. This mirrors the FSD `user.creation`/`user.gracePeriod` bug class: a second state-changing call (re-freezing before the first freeze has expired) recomputes a time-based commitment field using only the *new* call's parameters, discarding the timing implications of the *existing* commitment.

### Finding Description
`FreezeBalanceActuator.validate()` only checks that `frozenDuration` lies between `dynamicStore.getMinFrozenTime()` and `dynamicStore.getMaxFrozenTime()`: [1](#0-0) 

There is no check that compares the newly requested duration against the **remaining** lock time of any already-frozen, non-expired balance for the same resource. `execute()` then simply recomputes the expiry from `now`: [2](#0-1) 

and `AccountCapsule.setFrozenForBandwidth` (and the energy/TRON_POWER equivalents) unconditionally replaces the single `Frozen` entry's `expireTime`, combining the *new* frozen amount with the *old* frozen balance but discarding the old expiry: [3](#0-2) [4](#0-3) 

The same pattern exists in the native-contract path invoked by the TVM `freeze()` opcode, reachable by any deployed contract: [5](#0-4) [6](#0-5) 

Consequently, an account holding a frozen resource balance that has not yet reached its original `expireTime` can broadcast (or, via a contract, invoke the `freeze` opcode) another `FreezeBalanceContract` with a **shorter** duration. Because `setFrozenForBandwidth`/`setFrozenForEnergy` overwrite (rather than take the max of) the expiry, the combined frozen balance's unlock time is reset to `now + newDuration`, which can be *earlier* than the previously committed `expireTime`. This lets the account become eligible to unfreeze (via `UnfreezeBalanceActuator`, which only checks `frozen.getExpireTime() <= now`) the entire combined balance—including the portion that was supposed to remain locked longer—well before the originally committed period elapses: [7](#0-6) 

This is the mirror image of the FSD finding: instead of an incorrectly *extended* lock (denial of service to the user), java-tron's flawed re-freeze recalculation produces an incorrectly *shortened* lock (an economic-security violation, since bandwidth/energy weight and TRON_POWER voting weight are meant to be backed by TRX locked for a minimum duration).

### Impact Explanation
Bandwidth/Energy/TRON_POWER weight (and, historically, voting power) granted by `FreezeBalanceContract` is only economically meaningful if the corresponding TRX is actually locked for the committed duration. By manipulating the re-freeze/expiry overwrite behavior, a user can obtain resource weight for a large frozen balance while only actually being locked for the shortest configured duration, then unfreeze the entire balance early. This breaks the fund-locking guarantee underlying `addTotalWeight`/`getTronPowerFrozenBalance` and TRON_POWER-based voting, and can be used to withdraw stake earlier than the protocol intends, undermining the frozen-balance accounting invariant relied upon elsewhere in `Manager`/witness voting.

### Likelihood Explanation
The path is reachable by any unprivileged account: `FreezeBalanceActuator` is triggered by a plain, signed `FreezeBalanceContract` transaction, and `FreezeBalanceProcessor` is triggered by any deployed contract calling the `freeze()` TVM opcode—no special permission is required. The only precondition is holding an existing, unexpired frozen balance and issuing a second freeze call with a different (shorter) duration, which is a normal user operation. The severity of the practical impact depends on the configured `MinFrozenTime`/`MaxFrozenTime` window (if the network pins both to the same fixed value, the exploitable duration gap shrinks to zero); I was not able to fully confirm within this session whether `MinFrozenTime` and `MaxFrozenTime` are always equal in the current deployed configuration, so this should be validated against the live `DynamicPropertiesStore` values before treating likelihood as unconditional.

### Recommendation
When combining a new freeze with an existing, unexpired `Frozen` entry, the resulting `expireTime` should be the **maximum** of the existing `expireTime` and `now + newDuration`, not a blind overwrite. Apply the same fix symmetrically to `setFrozenForBandwidth`, `setFrozenForEnergy`, `setFrozenForTronPower` in `AccountCapsule`, and to the corresponding logic in `FreezeBalanceProcessor`/`FreezeBalanceV2Processor` (for the V2/native-contract paths), plus add an explicit validate()-time check disallowing a new freeze whose resulting expiry would be earlier than any already-committed, unexpired freeze for the same resource.

### Proof of Concept
1. Account A calls `FreezeBalanceContract` with `frozenBalance = 1000 TRX`, `frozenDuration = maxFrozenTime` (long lock) for `BANDWIDTH`.
   - `expireTime₁ = now₁ + maxFrozenTime * FROZEN_PERIOD` is stored via `setFrozenForBandwidth`.
2. Before `expireTime₁` is reached, Account A calls `FreezeBalanceContract` again with `frozenBalance = 1 TRX`, `frozenDuration = minFrozenTime` (shortest allowed lock) for `BANDWIDTH`.
   - `execute()` computes `expireTime₂ = now₂ + minFrozenTime * FROZEN_PERIOD`.
   - `setFrozenForBandwidth(1000 + 1, expireTime₂)` overwrites the stored `Frozen` entry, so the entire 1001 TRX now has expiry `expireTime₂ < expireTime₁`.
3. Once `now ≥ expireTime₂` (much earlier than the originally committed `expireTime₁`), Account A calls `UnfreezeBalanceActuator`, which succeeds because `frozen.getExpireTime() <= now`, releasing the full 1001 TRX far earlier than the initial 1000 TRX commitment implied.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/FreezeBalanceActuator.java (L69-94)
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

**File:** chainbase/src/main/java/org/tron/core/capsule/AccountCapsule.java (L1077-1089)
```java
  public void setFrozenForEnergy(long newFrozenBalanceForEnergy, long time) {
    Frozen newFrozenForEnergy = Frozen.newBuilder()
        .setFrozenBalance(newFrozenBalanceForEnergy)
        .setExpireTime(time)
        .build();

    AccountResource newAccountResource = getAccountResource().toBuilder()
        .setFrozenBalanceForEnergy(newFrozenForEnergy).build();

    this.account = this.account.toBuilder()
        .setAccountResource(newAccountResource)
        .build();
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

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L1927-1961)
```java
  public boolean freeze(DataWord receiverAddress, DataWord frozenBalance, DataWord resourceType) {
    Repository repository = getContractState().newRepositoryChild();
    byte[] owner = getContextAddress();
    byte[] receiver = receiverAddress.toTronAddress();

    increaseNonce();
    InternalTransaction internalTx = addInternalTx(null, owner, receiver,
        frozenBalance.longValue(), null,
        "freezeFor" + convertResourceToString(resourceType), nonce, null);

    FreezeBalanceParam param = new FreezeBalanceParam();
    param.setOwnerAddress(owner);
    param.setReceiverAddress(receiver);
    boolean needCheckFrozenTime = CommonParameter.getInstance()
        .getCheckFrozenTime() == 1; // for test
    param.setFrozenDuration(needCheckFrozenTime
        ? repository.getDynamicPropertiesStore().getMinFrozenTime() : 0);
    param.setResourceType(parseResourceCode(resourceType));
    try {
      FreezeBalanceProcessor processor = new FreezeBalanceProcessor();
      param.setFrozenBalance(frozenBalance.sValue().longValueExact());
      processor.validate(param, repository);
      processor.execute(param, repository);
      repository.commit();
      return true;
    } catch (ContractValidateException e) {
      logger.warn("TVM Freeze: validate failure. Reason: {}", e.getMessage());
    } catch (ArithmeticException e) {
      logger.warn("TVM Freeze: frozenBalance out of long range.");
    }
    if (internalTx != null) {
      internalTx.reject();
    }
    return false;
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/UnfreezeBalanceActuator.java (L435-447)
```java
    } else {
      switch (unfreezeBalanceContract.getResource()) {
        case BANDWIDTH:
          if (accountCapsule.getFrozenCount() <= 0) {
            throw new ContractValidateException("no frozenBalance(BANDWIDTH)");
          }

          long allowedUnfreezeCount = accountCapsule.getFrozenList().stream()
              .filter(frozen -> frozen.getExpireTime() <= now).count();
          if (allowedUnfreezeCount <= 0) {
            throw new ContractValidateException("It's not time to unfreeze(BANDWIDTH).");
          }
          break;
```
