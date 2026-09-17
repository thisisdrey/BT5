### Title
Committee kill-switches for unfreeze/cancel-unfreeze are enforced only in the actuator path, not in the TVM native-contract path - (File: `actuator/src/main/java/org/tron/core/vm/nativecontract/WithdrawExpireUnfreezeProcessor.java` / `CancelAllUnfreezeV2Processor.java`)

### Summary
The reported bug class is a two-step process where a committee-controlled "enabled" gate is checked in the first step but not re-checked in the second step, letting users complete an action after the feature was disabled. The closest reachable analog in java-tron is the pair `WithdrawExpireUnfreezeContract` / `CancelAllUnfreezeV2Contract`, which each have two independent invocation paths — a normal transaction actuator and a TVM native-contract call triggerable by any deployed smart contract — where only the actuator path enforces the corresponding committee feature switch.

### Finding Description
`WithdrawExpireUnfreezeActuator.validate()` explicitly checks the committee-controlled flag before allowing withdrawal of expired unfrozen balance: [1](#0-0) 

Likewise `CancelAllUnfreezeV2Actuator.validate()` checks `supportAllowCancelAllUnfreezeV2()`: [2](#0-1) 

However, the same operations are also reachable through TVM opcodes exposed to any smart contract via `Program.withdrawExpireUnfreeze()` and `Program.cancelAllUnfreezeV2Action()`, which delegate to `WithdrawExpireUnfreezeProcessor` and `CancelAllUnfreezeV2Processor` respectively: [3](#0-2) [4](#0-3) 

Neither `WithdrawExpireUnfreezeProcessor.validate()` nor `CancelAllUnfreezeV2Processor.validate()` checks `supportUnfreezeDelay()` / `supportAllowCancelAllUnfreezeV2()` at all — they only validate the address, account existence, and (post-fork) invalid-delegated-V2 timeout: [5](#0-4) [6](#0-5) 

This mirrors the reported pattern exactly: one code path (`redeemDollar` / actuator `validate()`) enforces the "enabled" gate, while the sibling code path that performs the same fund-affecting state transition (`collectRedemption` / TVM `Processor.validate()`) omits it.

### Impact Explanation
If the committee ever sets `supportUnfreezeDelay` or `supportAllowCancelAllUnfreezeV2` back to disabled after having enabled it (both are ordinary `DynamicPropertiesStore` parameters set via committee proposals, not immutable hard-fork flags), a user could still invoke `withdrawExpireUnfreeze()`/`cancelAllUnfreezeBalanceV2()` through a smart contract call and bypass the governance gate that the normal transaction path enforces, producing balance/vote-weight state changes the committee intended to block network-wide.

### Likelihood Explanation
Reachable by any unprivileged caller that deploys or calls a contract invoking the TVM native contract feature (`withdrawExpireUnfreeze()` / `cancelAllUnfreezeBalanceV2()`), requiring only a normal signed transaction — no special privileges needed. The precondition (committee disabling the flag after having enabled it) is under committee control and not something an attacker can trigger, so likelihood depends on governance actually toggling the parameter off, which is uncommon for this class of parameter but not architecturally prevented.

### Recommendation
Add the same feature-gate check (`dynamicStore.supportUnfreezeDelay()` for withdraw, `dynamicStore.supportAllowCancelAllUnfreezeV2()` for cancel) into `WithdrawExpireUnfreezeProcessor.validate()` and `CancelAllUnfreezeV2Processor.validate()` so the TVM-triggered path is consistent with the actuator path.

### Proof of Concept
1. Committee enables `supportUnfreezeDelay` (allowing `UnfreezeBalanceV2`/`WithdrawExpireUnfreezeContract`), user unfreezes balance via `UnfreezeBalanceV2Contract`, entries land in `UnfrozenV2List` with an expire time.
2. Committee later disables `supportUnfreezeDelay`.
3. A regular `WithdrawExpireUnfreezeContract` transaction is now rejected by `WithdrawExpireUnfreezeActuator.validate()` per [1](#0-0) .
4. The same user instead deploys/calls a contract that invokes the `withdrawExpireUnfreeze()` TVM builtin; `Program.withdrawExpireUnfreeze()` calls `WithdrawExpireUnfreezeProcessor.validate()`/`execute()`, which contains no `supportUnfreezeDelay()` check, so the withdrawal succeeds despite the committee having disabled the feature.

**Note**: I could not verify from the indexed code whether `supportUnfreezeDelay`/`supportAllowCancelAllUnfreezeV2` are ever toggled back off in practice by real committee proposals (I did not find explicit evidence either preventing or confirming a "disable after enable" governance flow), so the practical likelihood of this precondition being triggered is uncertain.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/WithdrawExpireUnfreezeActuator.java (L84-87)
```java
    if (!dynamicStore.supportUnfreezeDelay()) {
      throw new ContractValidateException("Not support WithdrawExpireUnfreeze transaction,"
          + " need to be opened by the committee");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/CancelAllUnfreezeV2Actuator.java (L130-133)
```java
    if (!dynamicStore.supportAllowCancelAllUnfreezeV2()) {
      throw new ContractValidateException("Not support CancelAllUnfreezeV2 transaction,"
          + " need to be opened by the committee");
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L2095-2124)
```java
  public long withdrawExpireUnfreeze() {
    Repository repository = getContractState().newRepositoryChild();
    byte[] owner = getContextAddress();

    increaseNonce();
    InternalTransaction internalTx = addInternalTx(null, owner, owner, 0, null,
        "withdrawExpireUnfreeze", nonce, null);

    try {
      WithdrawExpireUnfreezeParam param = new WithdrawExpireUnfreezeParam();
      param.setOwnerAddress(owner);

      WithdrawExpireUnfreezeProcessor processor = new WithdrawExpireUnfreezeProcessor();
      processor.validate(param, repository);
      long expireUnfreezeBalance = processor.execute(param, repository);
      repository.commit();
      if (internalTx != null) {
        internalTx.setValue(expireUnfreezeBalance);
      }
      return expireUnfreezeBalance;
    } catch (ContractValidateException e) {
      logger.warn("TVM WithdrawExpireUnfreeze: validate failure. Reason: {}", e.getMessage());
    } catch (ContractExeException e) {
      logger.warn("TVM WithdrawExpireUnfreeze: execute failure. Reason: {}", e.getMessage());
    }
    if (internalTx != null) {
      internalTx.reject();
    }
    return 0;
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L2126-2166)
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

      if (internalTx != null && CommonParameter.getInstance().saveCancelAllUnfreezeV2Details) {
        internalTx.setExtra(String.format("{\"%s\":%d,\"%s\":%d,\"%s\":%d}",
            BANDWIDTH.name(), result.getOrDefault(BANDWIDTH.name(), 0L),
            ENERGY.name(), result.getOrDefault(ENERGY.name(), 0L),
            TRON_POWER.name(), result.getOrDefault(TRON_POWER.name(), 0L)));
      }

      return true;
    } catch (ContractValidateException e) {
      logger.warn("TVM CancelAllUnfreezeV2: validate failure. Reason: {}", e.getMessage());
    } catch (ContractExeException e) {
      logger.warn("TVM CancelAllUnfreezeV2: execute failure. Reason: {}", e.getMessage());
    }
    if (internalTx != null) {
      internalTx.reject();
    }
    return false;
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
