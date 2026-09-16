## Finding: Fork-Gated `hasInvalidDelegatedV2()` Check Permanently Blocks Unfreeze/Withdraw for Affected Accounts

### Title
Fork-gated invalid-delegated-V2 guard permanently reverts stake withdrawal and unfreeze operations for affected accounts - (File: actuator/src/main/java/org/tron/core/vm/nativecontract/UnfreezeBalanceV2Processor.java)

### Summary
Similar to the RocketPool issue where a centrally-gated version check causes proposal/challenge finalization calls to revert forever once upgraded (leaving bonds permanently locked), java-tron gates its stake-unlock native contracts behind a fork check (`ForkController.instance().pass(VERSION_4_8_2_2)`) that, once active, unconditionally reverts `UnfreezeBalanceV2`, `CancelAllUnfreezeV2`, and `WithdrawExpireUnfreeze` for any account whose delegated V2 balance is in an "invalid" (negative) state.

### Finding Description
`AccountCapsule.hasInvalidDelegatedV2()` detects negative delegated V2 bandwidth/energy balances (a state that can arise from prior account lifecycle bugs, e.g. around self-destruct interactions, as covered by `StakeV2AfterSelfDestructTest`). Three TVM native-contract validators check this flag and, if the account is in this state and the `VERSION_4_8_2_2` fork has activated, call `MUtil.checkCPUTimeForInvalidDelegatedV2Balance()`, which throws `OutOfTimeException("CPU timeout for invalid delegated V2 balance")`: [1](#0-0) [2](#0-1) [3](#0-2) 

Before the fork activates, `pass(VERSION_4_8_2_2)` returns false and these same operations succeed even for accounts in the invalid-delegated-V2 state, as the unit test explicitly documents: [4](#0-3) 

Once the fork passes, the *same* accounts (with the *same* underlying corrupted/negative delegated balance) permanently fail every call to `withdrawExpireUnfreeze`, `cancelAllUnfreezeV2`, and `unfreezeBalanceV2` — the only TVM opcodes/native contracts capable of releasing previously frozen TRX or claiming already-expired unfreeze balances back to a spendable balance: [5](#0-4) [6](#0-5) 

There is no alternative path in the codebase that lets such an account reclaim its frozen or expired-unfreeze balance once this fork-gated guard rejects the call — the guard is unconditional and does not degrade or fall back to a safe partial operation; it simply always reverts for as long as `hasInvalidDelegatedV2()` remains true, which is permanent unless a future upgrade repairs the underlying delegated balance.

### Impact Explanation
Any account (including a smart contract account) that reaches the invalid-delegated-V2 state before the `VERSION_4_8_2_2` fork activates has its frozen TRX (bandwidth/energy stake) and any already-matured unfreeze-expired TRX permanently unreachable through the standard TVM native-contract calls after the fork. This is functionally identical to the RocketPool scenario: a value that was legitimately locked (frozen stake / pending unfreeze) becomes permanently stuck because the sole state-transition functions capable of releasing it are now unconditionally gated behind a version check with no fallback recovery mechanism, resulting in permanent freezing of funds for affected accounts.

### Likelihood Explanation
The precondition (an account with negative `delegatedFrozenV2BalanceForBandwidth`/`delegatedFrozenV2BalanceForEnergy`) is not something a normal user reaches through intended flows, but the very existence of `StakeV2AfterSelfDestructTest` in this codebase confirms the team is aware such states occur (e.g., via self-destruct interaction with delegated resources) and are already present in some accounts. Any account already in this state prior to the fork's activation is unconditionally and permanently affected the moment the fork passes, with no way for the account owner or the protocol to opt out or repair the state via a normal transaction.

### Recommendation
- Do not universally revert `UnfreezeBalanceV2Processor.validate`, `CancelAllUnfreezeV2Processor.validate`, and `WithdrawExpireUnfreezeProcessor.validate` for accounts with `hasInvalidDelegatedV2() == true` after the fork. Instead, provide a dedicated repair/reclaim path (e.g., clamp negative delegated balances to zero and allow the withdraw/unfreeze/cancel operation to proceed) so that legitimately frozen or unfreeze-expired TRX can still be recovered.
- Ensure any future version-gated behavioral change to a fund-release native contract includes a compatible migration or repair step executed automatically during the fork activation (similar to how `ForkController.upgrade()` migrates other state), rather than leaving affected accounts permanently blocked.
- Add a governance/committee-triggered remediation actuator that can directly zero out or correct invalid delegated V2 balances for affected addresses, unblocking their pending unfreeze/withdraw operations.

### Proof of Concept
1. Prior to `VERSION_4_8_2_2` fork activation, get an account into the `hasInvalidDelegatedV2()` state (negative `delegatedFrozenV2BalanceForBandwidth` or `delegatedFrozenV2BalanceForEnergy`), e.g. through the self-destruct interaction covered in `StakeV2AfterSelfDestructTest`.
2. Freeze/unfreeze TRX normally so the account accumulates an `UnFreezeV2` entry that will mature (`unfreezeExpireTime <= now`), or leave a `FreezeV2` balance to be unfrozen.
3. Wait for/trigger the `VERSION_4_8_2_2` hard fork to activate (`ForkController.pass(VERSION_4_8_2_2)` becomes true).
4. Call `withdrawExpireUnfreeze()`, `cancelAllUnfreezeV2()`, or `unfreezeBalanceV2()` via the TVM opcode/native contract for that account — every call now reverts with `OutOfTimeException("CPU timeout for invalid delegated V2 balance")`, as demonstrated directly by `invalidDelegatedBalancesBlockWithdrawAndCancelAfterFork` and `invalidDelegatedBalancesBlockUnfreezeAfterFork` in `StakeV2AfterSelfDestructTest.java`.
5. The account's frozen/unfreeze-expired TRX remains permanently locked, with no available transaction to reclaim it. [7](#0-6)

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/UnfreezeBalanceV2Processor.java (L86-94)
```java
    if (!checkUnfreezeBalance(accountCapsule, param.getUnfreezeBalance(), param.getResourceType())) {
      throw new ContractValidateException(
          "Invalid unfreeze_balance, [" + param.getUnfreezeBalance() + "] is invalid");
    }

    if (accountCapsule.hasInvalidDelegatedV2()) {
      MUtil.checkCPUTimeForInvalidDelegatedV2Balance();
    }
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/CancelAllUnfreezeV2Processor.java (L28-47)
```java
  public void validate(CancelAllUnfreezeV2Param param, Repository repo) throws ContractValidateException {
    if (repo == null) {
      throw new ContractValidateException(STORE_NOT_EXIST);
    }

    byte[] ownerAddress = param.getOwnerAddress();
    if (!DecodeUtil.addressValid(ownerAddress)) {
      throw new ContractValidateException("Invalid address");
    }
    AccountCapsule accountCapsule = repo.getAccount(ownerAddress);
    if (Objects.isNull(accountCapsule)) {
      String readableOwnerAddress = StringUtil.createReadableString(ownerAddress);
      throw new ContractValidateException(
          ACCOUNT_EXCEPTION_STR + readableOwnerAddress + NOT_EXIST_STR);
    }

    if (accountCapsule.hasInvalidDelegatedV2()) {
      MUtil.checkCPUTimeForInvalidDelegatedV2Balance();
    }
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/WithdrawExpireUnfreezeProcessor.java (L26-60)
```java
  public void validate(WithdrawExpireUnfreezeParam param, Repository repo) throws ContractValidateException {
    if (repo == null) {
      throw new ContractValidateException(STORE_NOT_EXIST);
    }

    byte[] ownerAddress = param.getOwnerAddress();
    DynamicPropertiesStore dynamicStore = repo.getDynamicPropertiesStore();
    if (!DecodeUtil.addressValid(ownerAddress)) {
      throw new ContractValidateException("Invalid address");
    }
    AccountCapsule accountCapsule = repo.getAccount(ownerAddress);
    if (Objects.isNull(accountCapsule)) {
      String readableOwnerAddress = StringUtil.createReadableString(ownerAddress);
      throw new ContractValidateException(ACCOUNT_EXCEPTION_STR
          + readableOwnerAddress + NOT_EXIST_STR);
    }

    long now = dynamicStore.getLatestBlockHeaderTimestamp();
    List<Protocol.Account.UnFreezeV2> unfrozenV2List = accountCapsule.getInstance()
        .getUnfrozenV2List();
    long totalWithdrawUnfreeze = getTotalWithdrawUnfreeze(unfrozenV2List, now);
    if (totalWithdrawUnfreeze < 0) {
      throw new ContractValidateException("no unFreeze balance to withdraw ");
    }
    try {
      LongMath.checkedAdd(accountCapsule.getBalance(), totalWithdrawUnfreeze);
    } catch (ArithmeticException e) {
      logger.debug(e.getMessage(), e);
      throw new ContractValidateException(e.getMessage());
    }

    if (accountCapsule.hasInvalidDelegatedV2()) {
      MUtil.checkCPUTimeForInvalidDelegatedV2Balance();
    }
  }
```

**File:** framework/src/test/java/org/tron/core/vm/nativecontract/StakeV2AfterSelfDestructTest.java (L62-101)
```java
  @Test
  public void invalidDelegatedBalancesBlockWithdrawAndCancelAfterFork() throws Exception {
    byte[] ownerAddress = address(1);
    Repository repository = mock(Repository.class);
    DynamicPropertiesStore dynamicStore = mock(DynamicPropertiesStore.class);
    when(repository.getDynamicPropertiesStore()).thenReturn(dynamicStore);
    when(dynamicStore.getLatestBlockHeaderTimestamp()).thenReturn(NOW);

    WithdrawExpireUnfreezeParam withdrawParam = new WithdrawExpireUnfreezeParam();
    withdrawParam.setOwnerAddress(ownerAddress);
    WithdrawExpireUnfreezeProcessor withdrawProcessor =
        new WithdrawExpireUnfreezeProcessor();
    CancelAllUnfreezeV2Param cancelParam = new CancelAllUnfreezeV2Param();
    cancelParam.setOwnerAddress(ownerAddress);
    CancelAllUnfreezeV2Processor cancelProcessor = new CancelAllUnfreezeV2Processor();

    ForkController forkController = mock(ForkController.class);
    try (MockedStatic<ForkController> fork = Mockito.mockStatic(ForkController.class)) {
      fork.when(ForkController::instance).thenReturn(forkController);
      when(forkController.pass(VERSION_4_8_2_2)).thenReturn(false);
      when(repository.getAccount(ownerAddress)).thenReturn(account(ownerAddress, -1, 0));
      withdrawProcessor.validate(withdrawParam, repository);
      cancelProcessor.validate(cancelParam, repository);
      when(repository.getAccount(ownerAddress)).thenReturn(account(ownerAddress, 0, -1));
      withdrawProcessor.validate(withdrawParam, repository);
      cancelProcessor.validate(cancelParam, repository);

      when(forkController.pass(VERSION_4_8_2_2)).thenReturn(true);
      when(repository.getAccount(ownerAddress)).thenReturn(account(ownerAddress, -1, 0));
      assertInvalidDelegatedV2Timeout(
          () -> withdrawProcessor.validate(withdrawParam, repository));
      assertInvalidDelegatedV2Timeout(
          () -> cancelProcessor.validate(cancelParam, repository));
      when(repository.getAccount(ownerAddress)).thenReturn(account(ownerAddress, 0, -1));
      assertInvalidDelegatedV2Timeout(
          () -> withdrawProcessor.validate(withdrawParam, repository));
      assertInvalidDelegatedV2Timeout(
          () -> cancelProcessor.validate(cancelParam, repository));
    }
  }
```

**File:** framework/src/test/java/org/tron/core/vm/nativecontract/StakeV2AfterSelfDestructTest.java (L103-135)
```java
  @Test
  public void invalidDelegatedBalancesBlockUnfreezeAfterFork() throws Exception {
    byte[] ownerAddress = address(1);
    Repository repository = mock(Repository.class);
    DynamicPropertiesStore dynamicStore = mock(DynamicPropertiesStore.class);
    when(repository.getDynamicPropertiesStore()).thenReturn(dynamicStore);
    when(dynamicStore.getLatestBlockHeaderTimestamp()).thenReturn(NOW);

    UnfreezeBalanceV2Param bandwidthParam = unfreezeParam(ownerAddress, BANDWIDTH);
    UnfreezeBalanceV2Param energyParam = unfreezeParam(ownerAddress, ENERGY);
    UnfreezeBalanceV2Processor processor = new UnfreezeBalanceV2Processor();

    ForkController forkController = mock(ForkController.class);
    try (MockedStatic<ForkController> fork = Mockito.mockStatic(ForkController.class)) {
      fork.when(ForkController::instance).thenReturn(forkController);
      when(forkController.pass(VERSION_4_8_2_2)).thenReturn(false);
      when(repository.getAccount(ownerAddress)).thenReturn(
          accountWithFrozenV2(ownerAddress, -1, 0, BANDWIDTH));
      processor.validate(bandwidthParam, repository);
      when(repository.getAccount(ownerAddress)).thenReturn(
          accountWithFrozenV2(ownerAddress, 0, -1, ENERGY));
      processor.validate(energyParam, repository);

      when(forkController.pass(VERSION_4_8_2_2)).thenReturn(true);
      when(repository.getAccount(ownerAddress)).thenReturn(
          accountWithFrozenV2(ownerAddress, -1, 0, BANDWIDTH));
      assertInvalidDelegatedV2Timeout(
          () -> processor.validate(bandwidthParam, repository));
      when(repository.getAccount(ownerAddress)).thenReturn(
          accountWithFrozenV2(ownerAddress, 0, -1, ENERGY));
      assertInvalidDelegatedV2Timeout(() -> processor.validate(energyParam, repository));
    }
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L2059-2093)
```java
  public boolean unfreezeBalanceV2(DataWord unfreezeBalance, DataWord resourceType) {
    Repository repository = getContractState().newRepositoryChild();
    byte[] owner = getContextAddress();

    increaseNonce();
    InternalTransaction internalTx = addInternalTx(null, owner, owner,
        unfreezeBalance.longValue(), null,
        "unfreezeBalanceV2For" + convertResourceToString(resourceType), nonce, null);

    try {
      UnfreezeBalanceV2Param param = new UnfreezeBalanceV2Param();
      param.setOwnerAddress(owner);
      param.setUnfreezeBalance(unfreezeBalance.sValue().longValueExact());
      param.setResourceType(parseResourceCodeV2(resourceType));

      UnfreezeBalanceV2Processor processor = new UnfreezeBalanceV2Processor();
      processor.validate(param, repository);
      long unfreezeExpireBalance = processor.execute(param, repository);
      repository.commit();
      if (unfreezeExpireBalance > 0) {
        increaseNonce();
        addInternalTx(null, owner, owner, unfreezeExpireBalance, null,
            "withdrawExpireUnfreezeWhileUnfreezing", nonce, null);
      }
      return true;
    } catch (ContractValidateException e) {
      logger.warn("TVM UnfreezeBalanceV2: validate failure. Reason: {}", e.getMessage());
    } catch (ArithmeticException e) {
      logger.warn("TVM UnfreezeBalanceV2: balance out of long range.");
    }
    if (internalTx != null) {
      internalTx.reject();
    }
    return false;
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/OperationActions.java (L859-867)
```java
  public static void withdrawExpireUnfreezeAction(Program program) {
    if (program.isStaticCall()) {
      throw new Program.StaticCallModificationException();
    }

    long expireUnfreezeBalance = program.withdrawExpireUnfreeze();
    program.stackPush(new DataWord(expireUnfreezeBalance));
    program.step();
  }
```
