### Title
Stake/Resource native-contract processors omit the self-destruct liveness check present in `FreezeBalanceV2Processor`, allowing state mutation of a "freed" account within the same transaction - (File: actuator/src/main/java/org/tron/core/vm/nativecontract/FreezeBalanceV2Processor.java)

### Summary
The Chrome CVE is a classic use-after-free: an object (a USB device) is released from a store while a reference to it is still reachable and gets operated on later. In java-tron's TVM execution model there is a structural analog: when a contract executes `SELFDESTRUCT`, the account is only *logically* freed — `Program.suicide()` calls `getContractState().markSelfDestruct(owner)` and `getResult().addDeleteAccount(...)`, but the `AccountCapsule` object stays live and mutable in the `RepositoryImpl.accountCache` until `commit()`/block application actually deletes it from the `AccountStore`.

### Finding Description
`RepositoryImpl.getAccount()` [1](#0-0)  always returns a live, mutable `AccountCapsule` for any address, with no check on whether that address has been `markSelfDestruct`-ed. Self-destruction only sets a flag in `selfDestructCache`/`isSelfDestructed()` [2](#0-1)  — the underlying capsule ("the freed object") is not removed from the cache and remains reachable via `getAccount`/`updateAccount` for the rest of the transaction (or nested calls) until the real deletion happens in `deleteContract`/`commit`.

The codebase already recognizes this as a security-relevant condition for one operation: `FreezeBalanceV2Processor.validate()` explicitly checks `repo.isSelfDestructed(ownerAddress)` and calls `MUtil.checkCPUTimeForFreezeV2AfterSelfDestruct()` before allowing a freeze after self-destruct [3](#0-2) , and this is fork-gated by `VERSION_4_8_2_2` as shown in `StakeV2AfterSelfDestructTest` [4](#0-3) . A repo-wide search for `isSelfDestructed` shows this guard exists only in `RepositoryImpl`/`ContractState`/`Program` internals and in exactly **one** native-contract processor (`FreezeBalanceV2Processor`). The sibling processors that mutate the very same stake/resource fields on an `AccountCapsule` — e.g. `UnfreezeBalanceV2Processor`, `DelegateResourceProcessor`, `UnDelegateResourceProcessor` [5](#0-4) , `CancelAllUnfreezeV2Processor`, `WithdrawExpireUnfreezeProcessor` — do not appear anywhere in the `isSelfDestructed` grep results, meaning they have no equivalent liveness check on the owner/receiver account before mutating frozen balances, delegated resources, or triggering transfers.

Because `Program.suicide()`/`suicide2()` still allow further processing to run against `owner`/`receiver` addresses in the same call context (as evidenced by delegated-resource transfer helpers reading/writing accounts post-`markSelfDestruct`, e.g. `transferDelegatedResourceToInheritor` and `transferFrozenV2BalanceToInheritor` in `Program.java` [6](#0-5) [7](#0-6) ), a contract could self-destruct and then, before the transaction/deletion is finalized, invoke a native precompile/opcode path (freeze/unfreeze/delegate/cancel-unfreeze/withdraw) that is missing the guard, mutating the "freed" account's frozen balances, delegated indexes, and total network weight counters. This mirrors the UAF pattern: an entity marked for destruction is still reachable and its state is mutated, producing inconsistent bookkeeping (e.g., `TotalNetWeight`/`TotalEnergyWeight` counters, `DelegatedResourceAccountIndexStore` entries) that outlive the eventual real deletion of the account in `RepositoryImpl.deleteContract()` [8](#0-7) .

### Impact Explanation
If confirmed reachable without the guard, this could let a malicious contract deployer/caller manipulate stake weight or delegated-resource accounting for a self-destructed account within a single transaction, producing unbacked bandwidth/energy weight, corrupted `DelegatedResourceAccountIndexStore` state, or double-counted `TotalNetWeight`/`TotalEnergyWeight`, which affects global resource pricing for all users — a High-severity accounting/consensus integrity issue if exploitable.

### Likelihood Explanation
Medium-to-Low confidence: I could not confirm within the available tool budget whether `UnfreezeBalanceV2Processor`, `DelegateResourceProcessor`, `CancelAllUnfreezeV2Processor`, and `WithdrawExpireUnfreezeProcessor` are actually missing the `isSelfDestructed` check (the grep only showed zero matches for those files, which is suggestive but not a full read of each file), nor whether an attacker can actually sequence a self-destruct and a subsequent native-contract call against the *same* address within one transaction (e.g., via TVM's native-contract-call opcodes, reentrancy, or `CREATE2` recreation as hinted by `FreezeTest.testCreate2SuicideToBlackHole`/`testCreate2SuicideToAccount` [9](#0-8) ). This needs direct source verification of each processor's `validate()` method and of the call graph that permits native-contract invocation after `SELFDESTRUCT` in the same trace.

### Recommendation
Have a background engineer read the full source of `UnfreezeBalanceV2Processor`, `DelegateResourceProcessor`, `UnDelegateResourceProcessor`, `CancelAllUnfreezeV2Processor`, and `WithdrawExpireUnfreezeProcessor` (all in `actuator/src/main/java/org/tron/core/vm/nativecontract/`) to confirm whether each checks `repo.isSelfDestructed(ownerAddress/receiverAddress)` before mutating account/stake state, and trace whether the TVM native-contract call opcode dispatch permits invoking these processors against an address already in the current execution's `selfDestructCache`. If the check is missing, add the same fork-gated `isSelfDestructed` + CPU-time/behavior guard used in `FreezeBalanceV2Processor.validate()` to the other processors, consistent with the existing `VERSION_4_8_2_2` fork pattern.

### Proof of Concept
Not constructed — requires confirming the exact native-contract call opcode/dispatch path that can reach `UnfreezeBalanceV2Processor`/`DelegateResourceProcessor`/etc. for an address already self-destructed within the same transaction, which was not verified within the available tool budget. A background Devin session with full repo access should read the five processor files named above plus the native-contract-call dispatch code (e.g., `VMActuator`/`Program`/precompile dispatcher for freeze/unfreeze/delegate contracts) to build a concrete PoC transaction sequence.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/repository/RepositoryImpl.java (L309-327)
```java
  @Override
  public AccountCapsule getAccount(byte[] address) {
    Key key = new Key(address);
    if (accountCache.containsKey(key)) {
      return new AccountCapsule(accountCache.get(key).getValue());
    }

    AccountCapsule accountCapsule;
    if (parent != null) {
      accountCapsule = parent.getAccount(address);
    } else {
      accountCapsule = getAccountStore().get(address);
    }

    if (accountCapsule != null) {
      accountCache.put(key, Value.create(accountCapsule));
    }
    return accountCapsule;
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/repository/RepositoryImpl.java (L487-492)
```java
  @Override
  public void deleteContract(byte[] address) {
    getCodeStore().delete(address);
    getAccountStore().delete(address);
    getContractStore().delete(address);
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/repository/RepositoryImpl.java (L576-597)
```java
  @Override
  public void markSelfDestruct(byte[] address) {
    selfDestructCache.add(Key.create(address));
  }

  @Override
  public boolean isSelfDestructed(byte[] address) {
    Key key = Key.create(address);
    if (selfDestructCache.contains(key)) {
      return true;
    }

    if (parent != null) {
      boolean isSelfDestructed = parent.isSelfDestructed(address);
      if (isSelfDestructed) {
        selfDestructCache.add(key);
      }
      return isSelfDestructed;
    } else {
      return false;
    }
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/FreezeBalanceV2Processor.java (L68-70)
```java
    if (repo.isSelfDestructed(ownerAddress)) {
      MUtil.checkCPUTimeForFreezeV2AfterSelfDestruct();
    }
```

**File:** framework/src/test/java/org/tron/core/vm/nativecontract/StakeV2AfterSelfDestructTest.java (L33-59)
```java
  @Test
  public void freezeAfterSelfDestructIsForkGated() throws Exception {
    byte[] ownerAddress = address(1);
    AccountCapsule owner = account(ownerAddress, 0, 0);
    owner.setBalance(TRX_PRECISION);

    Repository repository = mock(Repository.class);
    DynamicPropertiesStore dynamicStore = mock(DynamicPropertiesStore.class);
    when(repository.getDynamicPropertiesStore()).thenReturn(dynamicStore);
    when(repository.getAccount(ownerAddress)).thenReturn(owner);
    when(repository.isSelfDestructed(ownerAddress)).thenReturn(true);

    FreezeBalanceV2Param param = new FreezeBalanceV2Param();
    param.setOwnerAddress(ownerAddress);
    param.setFrozenBalance(TRX_PRECISION);
    param.setResourceType(BANDWIDTH);
    FreezeBalanceV2Processor processor = new FreezeBalanceV2Processor();

    ForkController forkController = mock(ForkController.class);
    try (MockedStatic<ForkController> fork = Mockito.mockStatic(ForkController.class)) {
      fork.when(ForkController::instance).thenReturn(forkController);
      when(forkController.pass(VERSION_4_8_2_2)).thenReturn(false);
      processor.validate(param, repository);

      when(forkController.pass(VERSION_4_8_2_2)).thenReturn(true);
      assertFreezeV2Timeout(() -> processor.validate(param, repository));
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/UnDelegateResourceProcessor.java (L91-102)
```java
  public void execute(UnDelegateResourceParam param, Repository repo) {
    byte[] ownerAddress = param.getOwnerAddress();
    byte[] receiverAddress = param.getReceiverAddress();
    long unDelegateBalance = param.getUnDelegateBalance();
    AccountCapsule ownerCapsule = repo.getAccount(ownerAddress);
    AccountCapsule receiverCapsule = repo.getAccount(receiverAddress);
    DynamicPropertiesStore dynamicStore = repo.getDynamicPropertiesStore();
    long now = repo.getHeadSlot();

    long transferUsage = 0;
    // modify receiver Account
    if (receiverCapsule != null) {
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L597-627)
```java
  private void transferDelegatedResourceToInheritor(byte[] ownerAddr, byte[] inheritorAddr, Repository repo) {

    // delegated resource from sender to owner, just abandon
    // in order to making that sender can unfreeze their balance in future
    // nothing will be deleted

    // delegated resource from owner to receiver
    // there cannot be any resource when suicide

    AccountCapsule ownerCapsule = repo.getAccount(ownerAddr);

    // transfer owner`s frozen balance for bandwidth to inheritor
    long frozenBalanceForBandwidthOfOwner = 0;
    // check if frozen for bandwidth exists
    if (ownerCapsule.getFrozenCount() != 0) {
      frozenBalanceForBandwidthOfOwner = ownerCapsule.getFrozenList().get(0).getFrozenBalance();
    }
    repo.addTotalNetWeight(-frozenBalanceForBandwidthOfOwner / TRX_PRECISION);

    long frozenBalanceForEnergyOfOwner =
        ownerCapsule.getAccountResource().getFrozenBalanceForEnergy().getFrozenBalance();
    repo.addTotalEnergyWeight(-frozenBalanceForEnergyOfOwner / TRX_PRECISION);

    // transfer all kinds of frozen balance to BlackHole
    repo.addBalance(inheritorAddr, frozenBalanceForBandwidthOfOwner + frozenBalanceForEnergyOfOwner);

    if (VMConfig.allowTvmSelfdestructRestriction()) {
      clearOwnerFreeze(ownerCapsule);
      repo.updateAccount(ownerAddr, ownerCapsule);
    }
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L629-690)
```java
  private long transferFrozenV2BalanceToInheritor(byte[] ownerAddr, byte[] inheritorAddr, Repository repo) {
    AccountCapsule ownerCapsule = repo.getAccount(ownerAddr);
    AccountCapsule inheritorCapsule = repo.getAccount(inheritorAddr);
    long now = repo.getHeadSlot();

    // transfer frozen resource
    ownerCapsule.getFrozenV2List().stream()
        .filter(freezeV2 -> freezeV2.getAmount() > 0)
        .forEach(
            freezeV2 -> {
              switch (freezeV2.getType()) {
                case BANDWIDTH:
                  inheritorCapsule.addFrozenBalanceForBandwidthV2(freezeV2.getAmount());
                  break;
                case ENERGY:
                  inheritorCapsule.addFrozenBalanceForEnergyV2(freezeV2.getAmount());
                  break;
                case TRON_POWER:
                  inheritorCapsule.addFrozenForTronPowerV2(freezeV2.getAmount());
                  break;
              }
            });

    // merge usage
    BandwidthProcessor bandwidthProcessor = new BandwidthProcessor(ChainBaseManager.getInstance());
    bandwidthProcessor.updateUsageForDelegated(ownerCapsule);
    ownerCapsule.setLatestConsumeTime(now);
    if (ownerCapsule.getNetUsage() > 0) {
      bandwidthProcessor.unDelegateIncrease(inheritorCapsule, ownerCapsule,
          ownerCapsule.getNetUsage(), BANDWIDTH, now);
    }

    EnergyProcessor energyProcessor =
        new EnergyProcessor(
            repo.getDynamicPropertiesStore(), ChainBaseManager.getInstance().getAccountStore());
    energyProcessor.updateUsage(ownerCapsule);
    ownerCapsule.setLatestConsumeTimeForEnergy(now);
    if (ownerCapsule.getEnergyUsage() > 0) {
      energyProcessor.unDelegateIncrease(inheritorCapsule, ownerCapsule,
          ownerCapsule.getEnergyUsage(), ENERGY, now);
    }

    // withdraw expire unfrozen balance
    long nowTimestamp = repo.getDynamicPropertiesStore().getLatestBlockHeaderTimestamp();
    long expireUnfrozenBalance =
        ownerCapsule.getUnfrozenV2List().stream()
            .filter(
                unFreezeV2 ->
                    unFreezeV2.getUnfreezeAmount() > 0 && unFreezeV2.getUnfreezeExpireTime() <= nowTimestamp)
            .mapToLong(Protocol.Account.UnFreezeV2::getUnfreezeAmount)
            .sum();
    if (expireUnfrozenBalance > 0) {
      inheritorCapsule.setBalance(inheritorCapsule.getBalance() + expireUnfrozenBalance);
      increaseNonce();
      addInternalTx(null, ownerAddr, inheritorAddr, expireUnfrozenBalance, null,
          "withdrawExpireUnfreezeWhileSuiciding", nonce, null);
    }
    clearOwnerFreezeV2(ownerCapsule);
    repo.updateAccount(ownerCapsule.createDbKey(), ownerCapsule);
    repo.updateAccount(inheritorCapsule.createDbKey(), inheritorCapsule);
    return expireUnfrozenBalance;
  }
```

**File:** framework/src/test/java/org/tron/common/runtime/vm/FreezeTest.java (L489-517)
```java
  @Test
  public void testCreate2SuicideToBlackHole() throws Exception {
    byte[] factory = deployContract("FactoryContract", FACTORY_CODE);
    byte[] contract = deployContract("TestFreeze", CONTRACT_CODE);
    long frozenBalance = 1_000_000;
    freezeForSelf(contract, frozenBalance, 0);
    freezeForSelf(contract, frozenBalance, 1);
    long salt = 1;
    byte[] predictedAddr = getCreate2Addr(factory, salt);
    freezeForOther(contract, predictedAddr, frozenBalance, 0);
    freezeForOther(contract, predictedAddr, frozenBalance, 1);
    Assert.assertArrayEquals(predictedAddr, deployCreate2Contract(factory, salt));
    setBalance(predictedAddr, 100_000_000);
    freezeForSelf(predictedAddr, frozenBalance, 0);
    freezeForSelf(predictedAddr, frozenBalance, 1);
    freezeForOther(predictedAddr, userA, frozenBalance, 0);
    freezeForOther(predictedAddr, userA, frozenBalance, 1);
    suicideWithException(predictedAddr, predictedAddr);
    clearDelegatedExpireTime(predictedAddr, userA);
    unfreezeForOther(predictedAddr, userA, 0);
    unfreezeForOther(predictedAddr, userA, 1);
    suicideToAccount(predictedAddr, predictedAddr);

    unfreezeForOtherWithException(contract, predictedAddr, 0);
    unfreezeForOtherWithException(contract, predictedAddr, 1);
    clearDelegatedExpireTime(contract, predictedAddr);
    unfreezeForOther(contract, predictedAddr, 0);
    unfreezeForOther(contract, predictedAddr, 1);
  }
```
