Confirmed: `UnfreezeBalanceV2Actuator` (the plain-transaction path reachable directly by any signed `UnfreezeBalanceV2Contract` broadcast) does not call `accountCapsule.hasInvalidDelegatedV2()` / `MUtil.checkCPUTimeForInvalidDelegatedV2Balance()`, while its TVM-native sibling `UnfreezeBalanceV2Processor.validate()` does. Likewise `WithdrawExpireUnfreezeActuator` (plain transaction) omits the same guard that its native-contract sibling `WithdrawExpireUnfreezeProcessor.validate()` and `CancelAllUnfreezeV2Processor.validate()` both enforce.

### Title
Missing invalid-delegated-V2-balance guard in transaction-level Unfreeze/Withdraw actuators lets a poisoned account bypass the post-fork safety check enforced by the TVM-native processors - (File: actuator/src/main/java/org/tron/core/actuator/UnfreezeBalanceV2Actuator.java, actuator/src/main/java/org/tron/core/actuator/WithdrawExpireUnfreezeActuator.java)

### Summary
This mirrors the reported pattern in the external report: one code path (`triggerDepeg`) enforces a state precondition before resolving/settling, while a sibling path that resolves the same state (`triggerEndEpoch`) omits the check. In java-tron, `UnfreezeBalanceV2Processor`, `WithdrawExpireUnfreezeProcessor`, and `CancelAllUnfreezeV2Processor` (the TVM native-contract implementations invoked from Solidity precompiles) all call `accountCapsule.hasInvalidDelegatedV2()` and, if true, `MUtil.checkCPUTimeForInvalidDelegatedV2Balance()` before proceeding [1](#0-0) [2](#0-1) [3](#0-2) . The regular actuators that execute the same contract types when submitted as ordinary signed transactions — `UnfreezeBalanceV2Actuator.validate()` and `WithdrawExpireUnfreezeActuator.validate()` — never call `hasInvalidDelegatedV2()` [4](#0-3) [5](#0-4) .

### Finding Description
`hasInvalidDelegatedV2()` detects an inconsistent account state where `getDelegatedFrozenV2BalanceForBandwidth()` or `getDelegatedFrozenV2BalanceForEnergy()` is negative [6](#0-5) . This condition is a known corruption scenario that can arise around contract self-destruct / stake-v2 interactions (see the dedicated regression test `StakeV2AfterSelfDestructTest#invalidDelegatedBalancesBlockWithdrawAndCancelAfterFork`, gated by `VERSION_4_8_2_2`) [7](#0-6) . Post-fork, the guard deliberately throws `OutOfTimeException("CPU timeout for invalid delegated V2 balance")` via `MUtil.checkCPUTimeForInvalidDelegatedV2Balance()` to halt processing when this corrupted state is detected, for both unfreezing and cancel/withdraw operations, when reached through the TVM opcode/precompile path.

However, `UnfreezeBalanceV2Contract`, `WithdrawExpireUnfreezeContract`, and `UnfreezeBalanceV2Contract`-family operations can also be submitted directly as top-level signed transactions, handled by `UnfreezeBalanceV2Actuator` and `WithdrawExpireUnfreezeActuator` respectively. Neither of these actuator classes performs the `hasInvalidDelegatedV2()` check anywhere in their `validate()` or `execute()` methods. Consequently, an account carrying the corrupted (negative) delegated V2 balance state can still unfreeze, cancel unfreezes, or withdraw expired unfrozen TRX by broadcasting the transaction-level contract directly, entirely bypassing the safety net that was specifically added to stop this class of operation.

### Impact Explanation
If the negative-delegated-balance corruption is reachable (as the dedicated fork-gated regression test suggests it is, at least for TVM-driven flows), the missing check in the plain actuators means the mitigation is incomplete: an attacker/account in this state can still execute unfreeze/withdraw/cancel operations through the ordinary transaction API, potentially resulting in resource-weight accounting drift (total net/energy weight miscalculation) or improper balance credit tied to the inconsistent delegated balance, undermining the very consistency check the native-contract sibling functions were hardened to enforce. This affects account balance/resource accounting correctness reachable from an ordinary broadcast transaction with no special privilege required.

### Likelihood Explanation
Likelihood depends entirely on how an account can be put into the `hasInvalidDelegatedV2()` state in the first place; this repository indicates it is tied to a specific self-destruct/stake-v2 interaction that was significant enough to warrant a dedicated post-fork guard and regression test (`StakeV2AfterSelfDestructTest`). Given that guard exists and is enforced in three separate native-contract processors but omitted in the two directly-reachable transaction actuators, the omission is a straightforward oversight of a security-relevant precondition when the operations were exposed as ordinary transaction types, matching the audit pattern of "one sibling function checks the depeg condition, the other resolving function does not."

### Recommendation
Add the same guard used in the native-contract processors to `UnfreezeBalanceV2Actuator.validate()` and `WithdrawExpireUnfreezeActuator.validate()`:
```java
if (accountCapsule.hasInvalidDelegatedV2()) {
  MUtil.checkCPUTimeForInvalidDelegatedV2Balance();
}
```
placed immediately after loading `accountCapsule`, mirroring `UnfreezeBalanceV2Processor.validate()` [8](#0-7)  and `WithdrawExpireUnfreezeProcessor.validate()` [9](#0-8) , so both the TVM and plain-transaction entry points enforce identical preconditions for the same underlying contract semantics. `CancelAllUnfreezeV2` should be checked for whether an equivalent plain-transaction actuator exists and receive the same treatment if so.

### Proof of Concept
1. Reach the state where `hasInvalidDelegatedV2()` returns true for an account (per the documented self-destruct/stake-v2 fork scenario covered by `StakeV2AfterSelfDestructTest`) [10](#0-9) .
2. Instead of triggering the operation via the TVM precompile path (`freezeBalanceV2`/`unfreezeBalanceV2`/`withdrawExpireUnfreeze`/`cancelAllUnfreezeV2` solidity functions, which route through the guarded `*Processor` classes), submit the equivalent top-level `UnfreezeBalanceV2Contract` or `WithdrawExpireUnfreezeContract` transaction directly.
3. Observe that `UnfreezeBalanceV2Actuator.validate()`/`execute()` and `WithdrawExpireUnfreezeActuator.validate()`/`execute()` proceed and complete successfully with no `hasInvalidDelegatedV2()` check, unlike the corresponding TVM path which reverts with `OutOfTimeException("CPU timeout for invalid delegated V2 balance")`.

I could not fully verify within the available context how an ordinary (non-contract) EOA account reaches `hasInvalidDelegatedV2() == true` outside the self-destruct/stake-v2 fork scenario documented in `StakeV2AfterSelfDestructTest`; this affects the practical likelihood assessment and would benefit from further investigation of `Program.java`'s self-destruct/stake-v2 transfer logic in a live Devin session with full repository access.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/UnfreezeBalanceV2Processor.java (L86-93)
```java
    if (!checkUnfreezeBalance(accountCapsule, param.getUnfreezeBalance(), param.getResourceType())) {
      throw new ContractValidateException(
          "Invalid unfreeze_balance, [" + param.getUnfreezeBalance() + "] is invalid");
    }

    if (accountCapsule.hasInvalidDelegatedV2()) {
      MUtil.checkCPUTimeForInvalidDelegatedV2Balance();
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/WithdrawExpireUnfreezeProcessor.java (L43-59)
```java
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

**File:** actuator/src/main/java/org/tron/core/actuator/UnfreezeBalanceV2Actuator.java (L103-185)
```java
  @Override
  public boolean validate() throws ContractValidateException {
    if (this.any == null) {
      throw new ContractValidateException(ActuatorConstant.CONTRACT_NOT_EXIST);
    }
    if (chainBaseManager == null) {
      throw new ContractValidateException(ActuatorConstant.STORE_NOT_EXIST);
    }
    AccountStore accountStore = chainBaseManager.getAccountStore();
    DynamicPropertiesStore dynamicStore = chainBaseManager.getDynamicPropertiesStore();
    if (!this.any.is(UnfreezeBalanceV2Contract.class)) {
      throw new ContractValidateException(
          "contract type error, expected type [UnfreezeBalanceContract], real type[" + any
              .getClass() + "]");
    }

    if (!dynamicStore.supportUnfreezeDelay()) {
      throw new ContractValidateException("Not support UnfreezeV2 transaction,"
          + " need to be opened by the committee");
    }

    final UnfreezeBalanceV2Contract unfreezeBalanceV2Contract;
    try {
      unfreezeBalanceV2Contract = this.any.unpack(UnfreezeBalanceV2Contract.class);
    } catch (InvalidProtocolBufferException e) {
      logger.debug(e.getMessage(), e);
      throw new ContractValidateException(e.getMessage());
    }

    byte[] ownerAddress = unfreezeBalanceV2Contract.getOwnerAddress().toByteArray();
    if (!DecodeUtil.addressValid(ownerAddress)) {
      throw new ContractValidateException("Invalid address");
    }

    AccountCapsule accountCapsule = accountStore.get(ownerAddress);
    if (accountCapsule == null) {
      String readableOwnerAddress = StringUtil.createReadableString(ownerAddress);
      throw new ContractValidateException(
          ACCOUNT_EXCEPTION_STR + readableOwnerAddress + "] does not exist");
    }

    long now = dynamicStore.getLatestBlockHeaderTimestamp();
    switch (unfreezeBalanceV2Contract.getResource()) {
      case BANDWIDTH:
        if (!checkExistFrozenBalance(accountCapsule, BANDWIDTH)) {
          throw new ContractValidateException("no frozenBalance(BANDWIDTH)");
        }
        break;
      case ENERGY:
        if (!checkExistFrozenBalance(accountCapsule, ENERGY)) {
          throw new ContractValidateException("no frozenBalance(Energy)");
        }
        break;
      case TRON_POWER:
        if (dynamicStore.supportAllowNewResourceModel()) {
          if (!checkExistFrozenBalance(accountCapsule, TRON_POWER)) {
            throw new ContractValidateException("no frozenBalance(TronPower)");
          }
        } else {
          throw new ContractValidateException("ResourceCode error.valid ResourceCode[BANDWIDTH、Energy]");
        }
        break;
      default:
        if (dynamicStore.supportAllowNewResourceModel()) {
          throw new ContractValidateException("ResourceCode error.valid ResourceCode[BANDWIDTH、Energy、TRON_POWER]");
        } else {
          throw new ContractValidateException("ResourceCode error.valid ResourceCode[BANDWIDTH、Energy]");
        }
    }

    if (!checkUnfreezeBalance(accountCapsule, unfreezeBalanceV2Contract, unfreezeBalanceV2Contract.getResource())) {
      throw new ContractValidateException(
          "Invalid unfreeze_balance, [" + unfreezeBalanceV2Contract.getUnfreezeBalance() + "] is error"
      );
    }

    int unfreezingCount = accountCapsule.getUnfreezingV2Count(now);
    if (UNFREEZE_MAX_TIMES <= unfreezingCount) {
      throw new ContractValidateException("Invalid unfreeze operation, unfreezing times is over limit");
    }

    return true;
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/WithdrawExpireUnfreezeActuator.java (L69-120)
```java
  public boolean validate() throws ContractValidateException {
    if (Objects.isNull(this.any)) {
      throw new ContractValidateException(ActuatorConstant.CONTRACT_NOT_EXIST);
    }
    if (Objects.isNull(chainBaseManager)) {
      throw new ContractValidateException(ActuatorConstant.STORE_NOT_EXIST);
    }
    AccountStore accountStore = chainBaseManager.getAccountStore();
    DynamicPropertiesStore dynamicStore = chainBaseManager.getDynamicPropertiesStore();
    if (!this.any.is(WithdrawExpireUnfreezeContract.class)) {
      throw new ContractValidateException(
          "contract type error, expected type [WithdrawExpireUnfreezeContract], real type[" + any
              .getClass() + "]");
    }

    if (!dynamicStore.supportUnfreezeDelay()) {
      throw new ContractValidateException("Not support WithdrawExpireUnfreeze transaction,"
          + " need to be opened by the committee");
    }

    final WithdrawExpireUnfreezeContract withdrawExpireUnfreezeContract;
    try {
      withdrawExpireUnfreezeContract = this.any.unpack(WithdrawExpireUnfreezeContract.class);
    } catch (InvalidProtocolBufferException e) {
      logger.debug(e.getMessage(), e);
      throw new ContractValidateException(e.getMessage());
    }
    byte[] ownerAddress = withdrawExpireUnfreezeContract.getOwnerAddress().toByteArray();
    if (!DecodeUtil.addressValid(ownerAddress)) {
      throw new ContractValidateException("Invalid address");
    }
    AccountCapsule accountCapsule = accountStore.get(ownerAddress);
    String readableOwnerAddress = StringUtil.createReadableString(ownerAddress);
    if (Objects.isNull(accountCapsule)) {
      throw new ContractValidateException(ACCOUNT_EXCEPTION_STR
          + readableOwnerAddress + NOT_EXIST_STR);
    }

    long now = dynamicStore.getLatestBlockHeaderTimestamp();
    List<UnFreezeV2> unfrozenV2List = accountCapsule.getInstance().getUnfrozenV2List();
    long totalWithdrawUnfreeze = getTotalWithdrawUnfreeze(unfrozenV2List, now);
    if (totalWithdrawUnfreeze <= 0) {
      throw new ContractValidateException("no unFreeze balance to withdraw ");
    }
    try {
      LongMath.checkedAdd(accountCapsule.getBalance(), totalWithdrawUnfreeze);
    } catch (ArithmeticException e) {
      logger.debug(e.getMessage(), e);
      throw new ContractValidateException(e.getMessage());
    }
    return true;
  }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/AccountCapsule.java (L1339-1341)
```java
  public boolean hasInvalidDelegatedV2() {
    return getDelegatedFrozenV2BalanceForBandwidth() < 0 || getDelegatedFrozenV2BalanceForEnergy() < 0;
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
