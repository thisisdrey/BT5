## Finding

### Title
Missing governance-gate check (`supportAllowCancelAllUnfreezeV2`) in TVM-callable `CancelAllUnfreezeV2Processor` allows bypassing the committee-controlled feature switch - (File: `actuator/src/main/java/org/tron/core/vm/nativecontract/CancelAllUnfreezeV2Processor.java`)

### Summary
The `CancelAllUnfreezeV2Contract` transaction path (`CancelAllUnfreezeV2Actuator`) enforces a committee-controlled feature gate (`supportAllowCancelAllUnfreezeV2()`) before allowing the "cancel all pending unfreeze" operation. The equivalent native-contract path invoked from the TVM opcode used by any smart contract (`Program.cancelAllUnfreezeV2Action()` → `CancelAllUnfreezeV2Processor.validate()`) omits this check entirely, so any deployed contract can perform the operation even when the committee has not enabled (or has disabled) it — the same class of bug as the reported `confirmWithdrawal` missing `whenWithdrawalNotPaused`.

### Finding Description
`CancelAllUnfreezeV2Actuator.validate()` requires the committee-controlled switch before permitting the operation: [1](#0-0) 

This flag is computed from two proposal-settable dynamic properties: [2](#0-1) 

However, the TVM-reachable native-contract processor for the exact same state transition performs no such check — it only validates address/account existence and an unrelated fork-based CPU-time guard: [3](#0-2) 

This processor is invoked directly from the `cancelAllUnfreezeV2` TVM opcode handler, which any deployed contract can trigger via `TriggerSmartContract`, with no intervening feature-flag check: [4](#0-3) [5](#0-4) 

The same inconsistency also exists for `WithdrawExpireUnfreezeActuator` vs. `WithdrawExpireUnfreezeProcessor`, and for `FreezeBalanceV2Actuator`/`UnfreezeBalanceV2Actuator` vs. `FreezeBalanceV2Processor`/`UnfreezeBalanceV2Processor`, all of which check `dynamicStore.supportUnfreezeDelay()` in the actuator but not in the corresponding TVM-callable processor: [6](#0-5) [7](#0-6) [8](#0-7) [9](#0-8) 

Note: opcode availability itself is separately gated by a hard-fork/config flag (`VMConfig`, e.g. `allowTvmFreezeV2`), which I could not fully trace within the available tool budget. That gate controls whether the *opcode exists at all* after a hard fork, but it is distinct from the committee proposal switches (`ALLOW_CANCEL_ALL_UNFREEZE_V2`, `UNFREEZE_DELAY_DAYS`) that the actuators check — those proposal switches are meant to be independently togglable by the committee to enable/disable the specific feature, and the TVM native-contract paths do not honor them.

### Impact Explanation
Once the relevant TVM opcodes are active on a network (post-fork), a smart-contract-based caller can invoke `cancelAllUnfreezeV2`, `withdrawExpireUnfreeze`, `freezeBalanceV2`, and `unfreezeBalanceV2` regardless of the committee's `AllowCancelAllUnfreezeV2` / `UnfreezeDelayDays` proposal settings, because only the plain-transaction actuator path enforces those switches. This defeats the purpose of the governance switches, which exist so the committee can disable a specific staking/unfreeze feature (e.g., if a bug is discovered in its accounting) without a hard fork. Because these operations directly mutate account balances and global `TotalNetWeight`/`TotalEnergyWeight`/`TotalTronPowerWeight` bookkeeping, bypassing the intended restriction can lead to inconsistent resource accounting or unintended fund movement while the feature is supposed to be halted.

### Likelihood Explanation
Any account can deploy a contract that calls these opcodes via a normal `TriggerSmartContract` transaction — this requires no special privilege, matching the "unprivileged transaction broadcaster/contract deployer" reachability bar. The precondition (committee having disabled/not-yet-enabled `ALLOW_CANCEL_ALL_UNFREEZE_V2` or `UNFREEZE_DELAY_DAYS` while the TVM opcode itself remains active) is plausible on networks that enabled `FreezeBalanceV2`-related TVM opcodes independently from the account-level staking proposals, but I could not conclusively confirm the exact ordering/dependency between the VMConfig hard-fork gate and these dynamic-property proposal gates within the current investigation.

### Recommendation
Add the same governance-flag checks used in the actuators to the corresponding TVM native-contract processors:
- `CancelAllUnfreezeV2Processor.validate()` should call `repo.getDynamicPropertiesStore().supportAllowCancelAllUnfreezeV2()` and throw `ContractValidateException` if false.
- `WithdrawExpireUnfreezeProcessor.validate()`, `FreezeBalanceV2Processor.validate()`, and `UnfreezeBalanceV2Processor.validate()` should call `supportUnfreezeDelay()` and throw if false, mirroring their actuator counterparts.

### Proof of Concept
1. Committee sets `ALLOW_CANCEL_ALL_UNFREEZE_V2 = 0` (or leaves it at default disabled) via the proposal mechanism, so `CancelAllUnfreezeV2Actuator` rejects direct `CancelAllUnfreezeV2Contract` transactions with "Not support CancelAllUnfreezeV2 transaction, need to be opened by the committee."
2. An attacker deploys a contract and calls it via `TriggerSmartContract`, which internally issues the `cancelAllUnfreezeV2` TVM instruction (`Program.cancelAllUnfreezeV2Action()`).
3. `CancelAllUnfreezeV2Processor.validate()` performs no `supportAllowCancelAllUnfreezeV2()` check, so the operation proceeds and mutates the account's `UnfrozenV2` list, balance, and global weight totals — completing successfully despite the feature being disabled by the committee.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/CancelAllUnfreezeV2Actuator.java (L130-133)
```java
    if (!dynamicStore.supportAllowCancelAllUnfreezeV2()) {
      throw new ContractValidateException("Not support CancelAllUnfreezeV2 transaction,"
          + " need to be opened by the committee");
    }
```

**File:** chainbase/src/main/java/org/tron/core/store/DynamicPropertiesStore.java (L2856-2858)
```java
  public boolean supportAllowCancelAllUnfreezeV2() {
    return getAllowCancelAllUnfreezeV2() == 1L && getUnfreezeDelayDays() > 0;
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

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L2126-2148)
```java
  public boolean cancelAllUnfreezeV2Action() {
    Repository repository = getContractState().newRepositoryChild();
    byte[] owner = getContextAddress();

    increaseNonce();
    InternalTransaction internalTx = addInternalTx(null, owner, owner, 0, null,
        "cancelAllUnfreezeV2", nonce, null);

    try {
      CancelAllUnfreezeV2Param param = new CancelAllUnfreezeV2Param();
      param.setOwnerAddress(owner);

      CancelAllUnfreezeV2Processor processor = new CancelAllUnfreezeV2Processor();
      processor.validate(param, repository);
      Map<String, Long> result = processor.execute(param, repository);
      repository.commit();

      if (result.get(VMConstant.WITHDRAW_EXPIRE_BALANCE) > 0) {
        increaseNonce();
        addInternalTx(null, owner, owner, result.get(VMConstant.WITHDRAW_EXPIRE_BALANCE), null,
            "withdrawExpireUnfreezeWhileCanceling", nonce, null);
      }

```

**File:** actuator/src/main/java/org/tron/core/vm/OperationActions.java (L869-877)
```java
  public static void cancelAllUnfreezeV2Action(Program program) {
    if (program.isStaticCall()) {
      throw new Program.StaticCallModificationException();
    }

    boolean result = program.cancelAllUnfreezeV2Action();
    program.stackPush(result ? DataWord.ONE() : DataWord.ZERO());
    program.step();
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/WithdrawExpireUnfreezeActuator.java (L84-87)
```java
    if (!dynamicStore.supportUnfreezeDelay()) {
      throw new ContractValidateException("Not support WithdrawExpireUnfreeze transaction,"
          + " need to be opened by the committee");
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

**File:** actuator/src/main/java/org/tron/core/actuator/FreezeBalanceV2Actuator.java (L107-110)
```java
    if (!dynamicStore.supportUnfreezeDelay()) {
      throw new ContractValidateException("Not support FreezeV2 transaction,"
          + " need to be opened by the committee");
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/FreezeBalanceV2Processor.java (L22-71)
```java
  public void validate(FreezeBalanceV2Param param, Repository repo) throws ContractValidateException {
    if (repo == null) {
      throw new ContractValidateException(STORE_NOT_EXIST);
    }

    byte[] ownerAddress = param.getOwnerAddress();
    if (!DecodeUtil.addressValid(ownerAddress)) {
      throw new ContractValidateException("Invalid address");
    }
    AccountCapsule ownerCapsule = repo.getAccount(ownerAddress);
    if (ownerCapsule == null) {
      String readableOwnerAddress = StringUtil.createReadableString(ownerAddress);
      throw new ContractValidateException(
          ACCOUNT_EXCEPTION_STR + readableOwnerAddress + "] does not exist");
    }
    long frozenBalance = param.getFrozenBalance();
    if (frozenBalance <= 0) {
      throw new ContractValidateException("FrozenBalance must be positive");
    } else if (frozenBalance < TRX_PRECISION) {
      throw new ContractValidateException("FrozenBalance must be greater than or equal to 1 TRX");
    } else if (frozenBalance > ownerCapsule.getBalance()) {
      throw new ContractValidateException(
          "FrozenBalance must be less than or equal to accountBalance");
    }

    // validate arg @resourceType
    switch (param.getResourceType()) {
      case BANDWIDTH:
      case ENERGY:
        break;
      case TRON_POWER:
        if (!repo.getDynamicPropertiesStore().supportAllowNewResourceModel()) {
          throw new ContractValidateException(
              "Unknown ResourceCode, valid ResourceCode[BANDWIDTH、ENERGY]");
        }
        break;
      default:
        if (repo.getDynamicPropertiesStore().supportAllowNewResourceModel()) {
          throw new ContractValidateException(
              "Unknown ResourceCode, valid ResourceCode[BANDWIDTH、ENERGY、TRON_POWER]");
        } else {
          throw new ContractValidateException(
              "Unknown ResourceCode, valid ResourceCode[BANDWIDTH、ENERGY]");
        }
    }

    if (repo.isSelfDestructed(ownerAddress)) {
      MUtil.checkCPUTimeForFreezeV2AfterSelfDestruct();
    }
  }
```
