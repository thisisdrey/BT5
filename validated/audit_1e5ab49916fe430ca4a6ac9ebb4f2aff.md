### Title
TVM `freeze` opcode unconditionally overwrites the account's resource-freeze expiration, allowing early bypass of the minimum frozen-time lock - (File: `actuator/src/main/java/org/tron/core/vm/program/Program.java`)

### Summary
`Program.freeze()` (reachable via the TVM `freeze` opcode from any deployed contract) always computes a new `FreezeBalanceParam.frozenDuration` of `0` in production (the `needCheckFrozenTime` flag is `for test` only), and `FreezeBalanceProcessor.execute()` then unconditionally overwrites the account's stored `expireTime` for the resource with `now + frozenDuration`, regardless of how far in the future the account's *existing* frozen-balance lock (set via a normal `FreezeBalanceContract` transaction subject to `minFrozenTime`) currently expires. This is the same bug class as the referenced NFTX finding: a timelock is recomputed as `now + newDuration` instead of only being extended (`max(existing, new)`), letting an actor shorten an existing lock.

### Finding Description
A contract account can freeze TRX for BANDWIDTH/ENERGY through the normal `FreezeBalanceContract` actuator, which enforces `minFrozenTime`/`maxFrozenTime` bounds via `FreezeBalanceActuator.validate()`: [1](#0-0) 

However, the same underlying logic is also reachable through the TVM `freeze` opcode (`Program.freeze`), which is invoked by contract bytecode and does not require normal user-side minimum lock enforcement in production: [2](#0-1) 

Here `needCheckFrozenTime` is explicitly commented `// for test`, and in production it evaluates to `false`, so `param.setFrozenDuration(0)` is always used for TVM-triggered freezes: [3](#0-2) 

`FreezeBalanceProcessor.execute()` then computes `expireTime = nowInMs + param.getFrozenDuration() * FROZEN_PERIOD` (i.e., `now + 0` for TVM calls) and calls `accountCapsule.setFrozenForBandwidth(...)` / `setFrozenForEnergy(...)` with this new expire time: [4](#0-3) 

`AccountCapsule.setFrozenForBandwidth`/`setFrozenForEnergy` unconditionally replace the stored `Frozen` record (balance + expireTime) rather than only extending the expiry if the new one is later: [5](#0-4) [6](#0-5) 

The same unconditional overwrite pattern exists for delegated resources in `delegateResource`/`DelegatedResourceCapsule.addFrozenBalanceForBandwidth/Energy`: [7](#0-6) [8](#0-7) 

**Attack path:** An attacker deploys a contract, freezes its own balance for BANDWIDTH/ENERGY via a normal `FreezeBalanceContract` transaction with a multi-day lock (subject to `minFrozenTime`). The contract then calls the TVM `freeze` opcode on itself (owner == receiver, self-acquire) with any small additional amount. Because `frozenDuration` is forced to `0` for TVM calls, `expireTime` is recomputed as `now`, unconditionally replacing the account's previously far-future `expireTime` for that resource — exactly like the NFTX bug where `_timelockMint` always sets `timelockFinish = block.timestamp + timelockLength` instead of only extending it. The contract can then immediately call `unfreeze`/`Program.unfreeze()` to reclaim the entire frozen balance before the originally-committed lock period elapses.

### Impact Explanation
This bypasses the protocol's frozen-time (bandwidth/energy staking) lock invariant, allowing a contract-controlled account to unlock and withdraw TRX earlier than the network-enforced `minFrozenTime`/`maxFrozenTime`. Since frozen balances back bandwidth/energy resource weight (and previously TRON Power/voting), being able to instantly shorten the lock lets an attacker manipulate resource acquisition/release timing to game resource pricing/voting weight, effectively bypassing an economic/anti-spam safeguard of the chain — a concrete unauthorized manipulation of account/resource state.

### Likelihood Explanation
Reachable by any unprivileged party who can deploy a contract and issue two ordinary transactions (a normal freeze transaction, then a contract call invoking the `freeze`/`unfreeze` opcodes). No special privileges, SR/witness/committee role, or timing races are required; `needCheckFrozenTime` is confirmed to be a test-only toggle disabled in production code paths.

### Recommendation
In `FreezeBalanceProcessor.execute()` (and the analogous delegate-resource path), when updating an existing frozen balance's `expireTime`, only extend it if the newly computed expiry is later than the currently stored one (`expireTime = max(existingExpireTime, now + frozenDuration*FROZEN_PERIOD)`), mirroring the NFTX mitigation of only updating the timelock when it is more restrictive, not less. Apply the same guard to `AccountCapsule.setFrozenForBandwidth/setFrozenForEnergy` and `DelegatedResourceCapsule.addFrozenBalanceForBandwidth/Energy`, or enforce `minFrozenTime` validation identically for TVM-triggered freezes as for actuator-triggered ones.

### Proof of Concept
1. Deploy contract `C`. Fund it with TRX.
2. Send a `FreezeBalanceContract` transaction from `C` freezing `X` TRX for `BANDWIDTH` with `frozenDuration` = `maxFrozenTime` (multi-day lock) — validated by `FreezeBalanceActuator.validate()`.
3. From `C`'s bytecode, invoke the TVM `freeze` opcode targeting itself (`receiverAddress == C`) with a small additional amount for `BANDWIDTH`.
   - `Program.freeze()` sets `frozenDuration = 0` (production path).
   - `FreezeBalanceProcessor.execute()` computes `expireTime = now`.
   - `accountCapsule.setFrozenForBandwidth(newTotalBalance, now)` overwrites the previous far-future expire time with `now`.
4. Immediately call the TVM `unfreeze` opcode (`Program.unfreeze` → `UnfreezeBalanceProcessor`) — validation now sees `expireTime <= now` and allows immediate unfreezing/withdrawal of the entire frozen balance, well before the originally committed multi-day lock.

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

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/FreezeBalanceProcessor.java (L137-158)
```java
  private void delegateResource(
      byte[] ownerAddress,
      byte[] receiverAddress,
      long frozenBalance,
      long expireTime,
      boolean isBandwidth,
      Repository repo) {
    byte[] key = DelegatedResourceCapsule.createDbKey(ownerAddress, receiverAddress);

    // insert or update DelegateResource
    DelegatedResourceCapsule delegatedResourceCapsule = repo.getDelegatedResource(key);
    if (delegatedResourceCapsule == null) {
      delegatedResourceCapsule = new DelegatedResourceCapsule(
          ByteString.copyFrom(ownerAddress),
          ByteString.copyFrom(receiverAddress));
    }
    if (isBandwidth) {
      delegatedResourceCapsule.addFrozenBalanceForBandwidth(frozenBalance, expireTime);
    } else {
      delegatedResourceCapsule.addFrozenBalanceForEnergy(frozenBalance, expireTime);
    }
    repo.updateDelegatedResource(key, delegatedResourceCapsule);
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

**File:** chainbase/src/main/java/org/tron/core/capsule/DelegatedResourceCapsule.java (L90-103)
```java
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
