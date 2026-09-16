### Title
UnfreezeBalanceV2Actuator never deducts the withdrawn amount from `frozenV2List`, allowing unbounded resource unfreeze - ([File: actuator/src/main/java/org/tron/core/actuator/UnfreezeBalanceV2Actuator.java])

### Summary
`UnfreezeBalanceV2Actuator.execute()` validates that the requested `unfreezeBalance` does not exceed the account's `FreezeV2` amount, but it never calls `updateAccountFrozenInfo` to subtract that amount from `frozenV2List`. As a result the underlying frozen principal recorded per-resource in `frozenV2List` is never reduced, even though `TotalNetWeight`/`TotalEnergyWeight`/`TotalTronPowerWeight` accounting is decremented and the account can repeat the unfreeze against the same (never-decreasing) frozen balance.

### Finding Description
`validate()` checks `checkUnfreezeBalance` against the current `FreezeV2.getAmount()` for the resource type [1](#0-0) . This is the sole authorization gate for how much a caller may unfreeze.

`execute()` performs the actual state mutation: it computes `unfreezeAmount` from already-expired unfreeze entries, appends the new unfreeze request to `unfrozenV2List` via `addUnfrozenV2List`, adjusts the network/energy/tron-power weight totals via `updateTotalResourceWeight`, and updates votes — but it never invokes `updateAccountFrozenInfo`, the only method that decrements `FreezeV2.getAmount()` in `frozenV2List`: [2](#0-1) 

The dedicated decrement method exists in the same class but is dead code from `execute()`'s perspective — it is only exercised directly by a unit test, never invoked by the actuator itself: [3](#0-2) [4](#0-3) 

For comparison, the equivalent TVM native-contract path `UnfreezeBalanceV2Processor.execute()` has the identical structure and also omits any call that decrements `FreezeV2.getAmount()`: [5](#0-4) 

Because `checkUnfreezeBalance` in `validate()` re-reads `frozenV2List` on every call [1](#0-0)  and that value is never reduced by `execute()`, an account can submit the maximum-allowed unfreeze transaction (up to `UNFREEZE_MAX_TIMES = 32` outstanding unfreeze entries, enforced at [6](#0-5) ) repeatedly against the same frozen principal, each time queuing a new `UnFreezeV2` entry that will mature and pay out TRX to `accountCapsule.getBalance()` via `unfreezeExpire()` [7](#0-6) , while only the resource-weight (`TotalNetWeight`/`TotalEnergyWeight`/`TotalTronPowerWeight`) bookkeeping is decremented and never the source-of-truth `frozenV2List.amount`.

### Impact Explanation
This is directly analogous to the "controller does not check remaining/insufficient balance after a loop/operation" class from the external report: the withdrawal-authorizing quantity (`frozenV2List.amount`) is checked at validation time but never actually decremented in execution, letting the withdrawer draw far more value than was ever locked. Here the concrete effect is that a user can repeatedly queue unfreeze operations against the same TRX principal (bounded only by the 32-outstanding-unfreeze-entries limit) and, after the unfreeze delay elapses, withdraw multiples of the amount they actually froze — creating unbacked TRX balance. This satisfies "unbacked balance / theft of funds" from a single signed transaction (`UnfreezeBalanceV2Contract`), reachable by any unprivileged account holder.

### Likelihood Explanation
High: any account that has frozen TRX for BANDWIDTH/ENERGY/TRON_POWER can trigger this by simply broadcasting `UnfreezeBalanceV2Contract` multiple times before waiting for `unfreezeDelayDays` to pass, up to 32 times per resource type, each round yielding a full unfreeze payout equal to their original frozen amount without ever reducing the checked `frozenV2List` value.

### Recommendation
In `UnfreezeBalanceV2Actuator.execute()` (and the corresponding `UnfreezeBalanceV2Processor.execute()` used by the TVM precompile), call `updateAccountFrozenInfo(freezeType, accountCapsule, unfreezeBalance)` (or the processor's equivalent) before persisting the account, so `frozenV2List.amount` is properly decremented and cannot be reused as unfreeze collateral.

### Proof of Concept
1. Freeze TRX for BANDWIDTH via `FreezeBalanceV2Contract`, resulting in `frozenV2List = [{type: BANDWIDTH, amount: X}]`.
2. Broadcast `UnfreezeBalanceV2Contract` requesting `unfreezeBalance = X` for BANDWIDTH. `validate()` passes because `X <= frozenV2List.amount (X)` [8](#0-7) . `execute()` adds an `UnFreezeV2{amount: X, expireTime: now+delay}` entry and decreases `TotalNetWeight`, but `frozenV2List.amount` remains `X` because `updateAccountFrozenInfo` is never called [9](#0-8) .
3. Immediately broadcast another `UnfreezeBalanceV2Contract` requesting `unfreezeBalance = X` again for BANDWIDTH. `validate()` still passes (frozenV2List.amount is still `X`), queuing a second `UnFreezeV2{amount: X}` entry.
4. Repeat up to `UNFREEZE_MAX_TIMES` (32) times.
5. After `unfreezeDelayDays` elapses, each queued `UnFreezeV2` entry matures and is paid out to `accountCapsule.getBalance()` via `unfreezeExpire()` on any subsequent unfreeze/withdraw call [10](#0-9) , so the account receives up to `32 * X` TRX in total despite having only frozen `X` TRX.

Note: because full end-to-end confirmation would require running the actuator against a live/test node and stepping through timestamps, this analysis is based on static code review; the described `execute()` code path and the absence of any decrement call to `frozenV2List` were directly verified in the source shown above.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/UnfreezeBalanceV2Actuator.java (L74-96)
```java
    AccountCapsule accountCapsule = accountStore.get(ownerAddress);
    long unfreezeAmount = this.unfreezeExpire(accountCapsule, now);
    long unfreezeBalance = unfreezeBalanceV2Contract.getUnfreezeBalance();

    if (dynamicStore.supportAllowNewResourceModel()
        && accountCapsule.oldTronPowerIsNotInitialized()) {
      accountCapsule.initializeOldTronPower();
    }

    ResourceCode freezeType = unfreezeBalanceV2Contract.getResource();

    long expireTime = this.calcUnfreezeExpireTime(now);
    accountCapsule.addUnfrozenV2List(freezeType, unfreezeBalance, expireTime);

    this.updateTotalResourceWeight(accountCapsule, unfreezeBalanceV2Contract, unfreezeBalance);
    this.updateVote(accountCapsule, unfreezeBalanceV2Contract, ownerAddress);

    if (dynamicStore.supportAllowNewResourceModel()
        && !accountCapsule.oldTronPowerIsInvalid()) {
      accountCapsule.invalidateOldTronPower();
    }

    accountStore.put(ownerAddress, accountCapsule);
```

**File:** actuator/src/main/java/org/tron/core/actuator/UnfreezeBalanceV2Actuator.java (L179-182)
```java
    int unfreezingCount = accountCapsule.getUnfreezingV2Count(now);
    if (UNFREEZE_MAX_TIMES <= unfreezingCount) {
      throw new ContractValidateException("Invalid unfreeze operation, unfreezing times is over limit");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/UnfreezeBalanceV2Actuator.java (L207-227)
```java
  public boolean checkUnfreezeBalance(AccountCapsule accountCapsule,
                                      final UnfreezeBalanceV2Contract unfreezeBalanceV2Contract,
                                      ResourceCode freezeType) {
    boolean checkOk = false;

    long frozenAmount = 0L;
    List<FreezeV2> freezeV2List = accountCapsule.getFrozenV2List();
    for (FreezeV2 freezeV2 : freezeV2List) {
      if (freezeV2.getType().equals(freezeType)) {
        frozenAmount = freezeV2.getAmount();
        break;
      }
    }

    if (unfreezeBalanceV2Contract.getUnfreezeBalance() > 0
        && unfreezeBalanceV2Contract.getUnfreezeBalance() <= frozenAmount) {
      checkOk = true;
    }

    return checkOk;
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/UnfreezeBalanceV2Actuator.java (L236-248)
```java
  public void updateAccountFrozenInfo(ResourceCode freezeType, AccountCapsule accountCapsule, long unfreezeBalance) {
    List<FreezeV2> freezeV2List = accountCapsule.getFrozenV2List();
    for (int i = 0; i < freezeV2List.size(); i++) {
      if (freezeV2List.get(i).getType().equals(freezeType)) {
        FreezeV2 freezeV2 = FreezeV2.newBuilder()
            .setAmount(freezeV2List.get(i).getAmount() - unfreezeBalance)
            .setType(freezeV2List.get(i).getType())
            .build();
        accountCapsule.updateFrozenV2List(i, freezeV2);
        break;
      }
    }
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/UnfreezeBalanceV2Actuator.java (L250-272)
```java
  public long unfreezeExpire(AccountCapsule accountCapsule, long now) {
    long unfreezeBalance = 0L;

    List<UnFreezeV2> unFrozenV2List = Lists.newArrayList();
    unFrozenV2List.addAll(accountCapsule.getUnfrozenV2List());
    Iterator<UnFreezeV2> iterator = unFrozenV2List.iterator();

    while (iterator.hasNext()) {
      UnFreezeV2 next = iterator.next();
      if (next.getUnfreezeExpireTime() <= now) {
        unfreezeBalance += next.getUnfreezeAmount();
        iterator.remove();
      }
    }

    accountCapsule.setInstance(
        accountCapsule.getInstance().toBuilder()
            .setBalance(accountCapsule.getBalance() + unfreezeBalance)
            .clearUnfrozenV2()
            .addAllUnfrozenV2(unFrozenV2List).build()
    );
    return unfreezeBalance;
  }
```

**File:** framework/src/test/java/org/tron/core/actuator/UnfreezeBalanceV2ActuatorTest.java (L634-664)
```java
  @Test
  public void testUnfreezeBalanceUpdateAccountFrozenInfo() {
    long now = System.currentTimeMillis();
    dbManager.getDynamicPropertiesStore().saveLatestBlockHeaderTimestamp(now);
    dbManager.getDynamicPropertiesStore().saveUnfreezeDelayDays(30);
    dbManager.getDynamicPropertiesStore().saveAllowDelegateResource(1);
    long unfreezeBalance = frozenBalance - 1;

    AccountCapsule accountCapsule = dbManager.getAccountStore()
            .get(ByteArray.fromHexString(OWNER_ADDRESS));
    accountCapsule.addFrozenBalanceForBandwidthV2(frozenBalance);

    dbManager.getAccountStore().put(accountCapsule.createDbKey(), accountCapsule);
    UnfreezeBalanceV2Actuator actuator = new UnfreezeBalanceV2Actuator();
    actuator.setChainBaseManager(dbManager.getChainBaseManager())
            .setAny(getContractForBandwidthV2(OWNER_ADDRESS, unfreezeBalance));

    BalanceContract.UnfreezeBalanceV2Contract unfreezeBalanceV2Contract =
            BalanceContract.UnfreezeBalanceV2Contract.newBuilder()
            .setOwnerAddress(ByteString.copyFrom(ByteArray.fromHexString(OWNER_ADDRESS)))
            .setUnfreezeBalance(unfreezeBalance)
            .setResource(ResourceCode.TRON_POWER)
            //.setReceiverAddress(ByteString.copyFrom(ByteArray.fromHexString(RECEIVER_ADDRESS)))
            .build();

    actuator.updateAccountFrozenInfo(
            ResourceCode.BANDWIDTH, accountCapsule, unfreezeBalance
    );

    Assert.assertEquals(1, accountCapsule.getAllFrozenBalanceForBandwidth());
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/UnfreezeBalanceV2Processor.java (L123-151)
```java
  public long execute(UnfreezeBalanceV2Param param, Repository repo) {
    byte[] ownerAddress = param.getOwnerAddress();
    long unfreezeBalance = param.getUnfreezeBalance();
    VoteRewardUtil.withdrawReward(ownerAddress, repo);

    AccountCapsule accountCapsule = repo.getAccount(ownerAddress);
    long now = repo.getDynamicPropertiesStore().getLatestBlockHeaderTimestamp();

    long unfreezeExpireBalance = this.unfreezeExpire(accountCapsule, now);

    if (repo.getDynamicPropertiesStore().supportAllowNewResourceModel()
        && accountCapsule.oldTronPowerIsNotInitialized()) {
      accountCapsule.initializeOldTronPower();
    }

    long expireTime = this.calcUnfreezeExpireTime(now, repo);
    accountCapsule.addUnfrozenV2List(param.getResourceType(), unfreezeBalance, expireTime);

    this.updateTotalResourceWeight(accountCapsule, param.getResourceType(), unfreezeBalance, repo);
    this.updateVote(accountCapsule, param.getResourceType(), ownerAddress, repo);

    if (repo.getDynamicPropertiesStore().supportAllowNewResourceModel()
        && !accountCapsule.oldTronPowerIsInvalid()) {
      accountCapsule.invalidateOldTronPower();
    }

    repo.updateAccount(accountCapsule.createDbKey(), accountCapsule);
    return unfreezeExpireBalance;
  }
```
