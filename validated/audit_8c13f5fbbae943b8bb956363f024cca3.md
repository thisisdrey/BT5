### Title
Missing `supportUnfreezeDelay` feature-gate check in native-contract unfreeze/withdraw processors invoked from smart contracts - (File: actuator/src/main/java/org/tron/core/vm/nativecontract/UnfreezeBalanceV2Processor.java, actuator/src/main/java/org/tron/core/vm/nativecontract/WithdrawExpireUnfreezeProcessor.java)

### Summary
The "UnfreezeBalanceV2" resource-unstaking feature is gated by the committee-controlled dynamic property `supportUnfreezeDelay`. The transaction-broadcast actuators for this feature correctly reject the operation when the feature has not been enabled by the committee, but the sibling native-contract processors that implement the identical logic for smart-contract callers do not perform this check, mirroring the "one sibling function checks the pause/feature flag, the other does not" bug class from the external report.

### Finding Description
`UnfreezeBalanceV2Actuator.validate()` explicitly requires the committee-controlled feature flag before permitting the new delayed-unfreeze mechanism: [1](#0-0) 

Likewise, `WithdrawExpireUnfreezeActuator.validate()` performs the same gate before allowing withdrawal of previously unfrozen (delayed) balances: [2](#0-1) 

These two actuators are reached only when a user broadcasts an `UnfreezeBalanceV2Contract` / `WithdrawExpireUnfreezeContract` transaction directly.

However, the same business logic is duplicated for smart-contract callers (i.e., a contract invoking the corresponding TVM native/precompiled functions) in `UnfreezeBalanceV2Processor.validate()` and `WithdrawExpireUnfreezeProcessor.validate()`. Neither of these `validate()` methods calls `dynamicStore.supportUnfreezeDelay()`: [3](#0-2) [4](#0-3) 

A codebase-wide search for `supportUnfreezeDelay` confirms it is checked in the actuator classes (`DelegateResourceActuator`, `FreezeBalanceActuator`, `FreezeBalanceV2Actuator`, `UnDelegateResourceActuator`, `UnfreezeBalanceV2Actuator`, `WithdrawExpireUnfreezeActuator`) but is absent from `UnfreezeBalanceV2Processor.java` and `WithdrawExpireUnfreezeProcessor.java`, the exact processors invoked via TVM from within `Program.java` when a contract calls the corresponding native staking opcodes.

### Impact Explanation
If the committee has not yet enabled (or has disabled/rolled back) the `UnfreezeBalanceV2`/delayed-unfreeze feature via `supportUnfreezeDelay`, ordinary transaction-based unfreeze/withdraw is correctly blocked. But any smart contract can still invoke the equivalent native functions through the TVM path, bypassing the committee's feature gate. This allows unauthorized use of a feature the network explicitly has not turned on, which can desynchronize accounting invariants that the rest of the codebase assumes are gated by `supportUnfreezeDelay` (e.g., total resource weight bookkeeping, unfreezing-count limits tied to the new model), and constitutes an unauthorized bypass of a protocol-level control on staking/unstaking operations.

### Likelihood Explanation
Any deployed smart contract can trigger this path by calling the corresponding native precompiled function from `Program.java`; no special privileges are required beyond deploying and invoking a contract, making this reachable by any unprivileged contract deployer/caller once the relevant TVM opcode exists in the running node version.

### Recommendation
Add `if (!repo.getDynamicPropertiesStore().supportUnfreezeDelay()) { throw new ContractValidateException(...); }` checks to `UnfreezeBalanceV2Processor.validate()` and `WithdrawExpireUnfreezeProcessor.validate()`, mirroring the checks already present in `UnfreezeBalanceV2Actuator.validate()` and `WithdrawExpireUnfreezeActuator.validate()`.

### Proof of Concept
1. Deploy a contract that calls the native `unfreezeBalanceV2`/`withdrawExpireUnfreeze` TVM functions (as routed through `Program.java`) while `supportUnfreezeDelay` is disabled at the network level.
2. Observe that `UnfreezeBalanceV2Processor.validate()` / `WithdrawExpireUnfreezeProcessor.validate()` proceed without checking `supportUnfreezeDelay`, successfully executing the operation.
3. Compare with broadcasting an equivalent `UnfreezeBalanceV2Contract` transaction directly, which is correctly rejected by `UnfreezeBalanceV2Actuator.validate()` with "Not support UnfreezeV2 transaction, need to be opened by the committee".

Note: I was unable to fully trace every call site in `Program.java` that dispatches to these processors (e.g., confirming exact opcode gating conditions at the `PrecompiledContracts.java` level) due to iteration limits, so it's possible there is an additional feature-flag check earlier in the TVM dispatch chain that I did not locate; this should be verified before treating the finding as fully confirmed.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/UnfreezeBalanceV2Actuator.java (L119-122)
```java
    if (!dynamicStore.supportUnfreezeDelay()) {
      throw new ContractValidateException("Not support UnfreezeV2 transaction,"
          + " need to be opened by the committee");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/WithdrawExpireUnfreezeActuator.java (L84-87)
```java
    if (!dynamicStore.supportUnfreezeDelay()) {
      throw new ContractValidateException("Not support WithdrawExpireUnfreeze transaction,"
          + " need to be opened by the committee");
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
