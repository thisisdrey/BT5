### Title
TVM native contract path for `withdrawExpireUnfreeze()` bypasses the committee-gated `supportUnfreezeDelay()` check enforced at the transaction level - (File: actuator/src/main/java/org/tron/core/vm/nativecontract/WithdrawExpireUnfreezeProcessor.java)

### Summary
`WithdrawExpireUnfreezeContract` (a broadcastable transaction type handled by `WithdrawExpireUnfreezeActuator`) and the TVM-native equivalent invoked through the `withdrawExpireUnfreeze()` Solidity-level call (handled by `WithdrawExpireUnfreezeProcessor` via `Program.withdrawExpireUnfreeze()`) are supposed to implement the same feature, gated by the committee-controlled chain parameter checked with `dynamicStore.supportUnfreezeDelay()`. The actuator enforces this gate; the TVM native-contract processor does not, mirroring the reported ERC4626 class of bug where overridden/duplicated code paths enforce restrictions inconsistently, causing one path to behave as if the feature is always enabled.

### Finding Description
`WithdrawExpireUnfreezeActuator.validate()` explicitly rejects the operation unless the committee has enabled the feature: [1](#0-0) 

The TVM native-contract equivalent, `WithdrawExpireUnfreezeProcessor.validate()`, performs balance/overflow checks but never calls `dynamicStore.supportUnfreezeDelay()`: [2](#0-1) 

This processor is invoked directly from `Program.withdrawExpireUnfreeze()`, which is reachable by any smart contract executing the `withdrawExpireUnfreeze` native TVM operation: [3](#0-2) 
which is in turn dispatched from the opcode handler `withdrawExpireUnfreezeAction`: [4](#0-3) 

Similarly, `UnfreezeBalanceV2Processor` (the native-contract counterpart used to create the `UnFreezeV2` entries that `withdrawExpireUnfreeze` later consumes) only checks `supportAllowNewResourceModel()`, not `supportUnfreezeDelay()`: [5](#0-4) 

The result is the same inconsistency pattern described in the report: one implementation of a feature ("mint"/"deposit"/"withdraw" in the ERC4626 case; the transaction-level actuator here) enforces a gating condition, while a second, parallel implementation of the *same* logical operation ("maxMint"/"maxDeposit"/"maxWithdraw" in the ERC4626 case; the TVM native-contract processor here) does not, so behavior differs depending on which entry point is used.

### Impact Explanation
If the chain parameter that `supportUnfreezeDelay()` reflects has not been enabled by the committee (i.e. the delayed-unfreeze / freeze-v2 withdraw-expire feature is intentionally disabled network-wide), a smart contract can still call `withdrawExpireUnfreeze()` (and accumulate `UnFreezeV2` entries via `unfreezeBalanceV2()`) through the TVM path and have funds released, while the same operation submitted as a plain `WithdrawExpireUnfreezeContract` transaction from an EOA would be rejected with "Not support WithdrawExpireUnfreeze transaction, need to be opened by the committee". This is a feature-flag/committee-control bypass: contract-mediated transactions can access chain behavior the network operators have not yet turned on, which the report's category maps to "unauthorized operation" relative to the intended governance gate.

### Likelihood Explanation
Any unprivileged account can deploy a contract and call `withdrawExpireUnfreeze()`/`unfreezeBalanceV2()` through it (`isStaticCall` is the only precondition checked before dispatch, per `OperationActions.withdrawExpireUnfreezeAction`), so exploitation requires no special privilege beyond deploying and calling a contract, making this readily reachable from a broadcastable transaction.

### Recommendation
Add the same `dynamicStore.supportUnfreezeDelay()` check to `WithdrawExpireUnfreezeProcessor.validate()` (and confirm consistency for `UnfreezeBalanceV2Processor` with respect to any related delay-feature flags) so the TVM native-contract path enforces identical committee-gating logic as `WithdrawExpireUnfreezeActuator`.

### Proof of Concept
1. Deploy a simple contract that calls the native TVM function `withdrawExpireUnfreeze()` (as exercised in `FreezeV2Test.triggerWithdrawExpireUnfreeze`, see [6](#0-5) ).
2. On a network/state where `supportUnfreezeDelay()` returns `false` (feature not enabled by committee), submit a plain `WithdrawExpireUnfreezeContract` transaction from an EOA with expired `UnFreezeV2` entries — observe it is rejected per `WithdrawExpireUnfreezeActuator.validate()` line 84-87.
3. Submit the equivalent operation by calling the contract's `withdrawExpireUnfreeze()` TVM function instead — because `WithdrawExpireUnfreezeProcessor.validate()` never checks `supportUnfreezeDelay()`, the call succeeds and balance is withdrawn, demonstrating the bypass.

Note: I was unable to directly inspect the exact definition/default value of `supportUnfreezeDelay()`/`UNFREEZE_DELAY_DAYS` in `DynamicPropertiesStore.java` within the indexed content (the grep for that file returned no matches, likely due to index truncation on that large file). I recommend a Devin session with full file access confirm the default state of this flag and its exact semantics before finalizing severity.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/WithdrawExpireUnfreezeActuator.java (L84-87)
```java
    if (!dynamicStore.supportUnfreezeDelay()) {
      throw new ContractValidateException("Not support WithdrawExpireUnfreeze transaction,"
          + " need to be opened by the committee");
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/WithdrawExpireUnfreezeProcessor.java (L26-59)
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
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L2095-2123)
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

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/UnfreezeBalanceV2Processor.java (L34-94)
```java
  public void validate(UnfreezeBalanceV2Param param, Repository repo)
      throws ContractValidateException {
    if (repo == null) {
      throw new ContractValidateException(STORE_NOT_EXIST);
    }

    byte[] ownerAddress = param.getOwnerAddress();
    DynamicPropertiesStore dynamicStore = repo.getDynamicPropertiesStore();
    if (!DecodeUtil.addressValid(ownerAddress)) {
      throw new ContractValidateException("Invalid address");
    }
    AccountCapsule accountCapsule = repo.getAccount(ownerAddress);
    if (accountCapsule == null) {
      String readableOwnerAddress = StringUtil.createReadableString(ownerAddress);
      throw new ContractValidateException(
          ACCOUNT_EXCEPTION_STR + readableOwnerAddress + "] does not exist");
    }
    long now = dynamicStore.getLatestBlockHeaderTimestamp();
    int unfreezingCount = accountCapsule.getUnfreezingV2Count(now);
    if (UnfreezeBalanceV2Actuator.getUNFREEZE_MAX_TIMES() <= unfreezingCount) {
      throw new ContractValidateException("Invalid unfreeze operation, unfreezing times is over limit");
    }
    switch (param.getResourceType()) {
      case BANDWIDTH:
        // validate frozen balance
        if (!this.checkExistFrozenBalance(accountCapsule, Common.ResourceCode.BANDWIDTH)) {
          throw new ContractValidateException("no frozenBalance(BANDWIDTH)");
        }
        break;
      case ENERGY:
        // validate frozen balance
        if (!this.checkExistFrozenBalance(accountCapsule, Common.ResourceCode.ENERGY)) {
          throw new ContractValidateException("no frozenBalance(ENERGY)");
        }
        break;
      case TRON_POWER:
        if (dynamicStore.supportAllowNewResourceModel()) {
          if (!this.checkExistFrozenBalance(accountCapsule, Common.ResourceCode.TRON_POWER)) {
            throw new ContractValidateException("no frozenBalance(TRON_POWER)");
          }
        } else {
          throw new ContractValidateException("Unknown ResourceCode, valid ResourceCode[BANDWIDTH、ENERGY]");
        }
        break;
      default:
        if (dynamicStore.supportAllowNewResourceModel()) {
          throw new ContractValidateException("Unknown ResourceCode, valid ResourceCode[BANDWIDTH、ENERGY、TRON_POWER]");
        } else {
          throw new ContractValidateException("Unknown ResourceCode, valid ResourceCode[BANDWIDTH、ENERGY]");
        }
    }

    if (!checkUnfreezeBalance(accountCapsule, param.getUnfreezeBalance(), param.getResourceType())) {
      throw new ContractValidateException(
          "Invalid unfreeze_balance, [" + param.getUnfreezeBalance() + "] is invalid");
    }

    if (accountCapsule.hasInvalidDelegatedV2()) {
      MUtil.checkCPUTimeForInvalidDelegatedV2Balance();
    }
  }
```

**File:** framework/src/test/java/org/tron/common/runtime/vm/FreezeV2Test.java (L258-263)
```java
  private TVMTestResult triggerWithdrawExpireUnfreeze(
      byte[] callerAddr, byte[] contractAddr, contractResult expectedResult, Consumer<byte[]> check)
      throws Exception {
    return triggerContract(
        callerAddr, contractAddr, fee, expectedResult, check, "withdrawExpireUnfreeze()");
  }
```
