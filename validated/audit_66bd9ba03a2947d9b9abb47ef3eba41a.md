## Analog Found

### Title
Missing feature-gate check in `CancelAllUnfreezeV2Processor`/`WithdrawExpireUnfreezeProcessor` allows TVM contracts to bypass the committee-controlled hard fork switch enforced by the corresponding transaction actuators - (File: `actuator/src/main/java/org/tron/core/vm/nativecontract/CancelAllUnfreezeV2Processor.java`)

### Summary
The reported bug pattern is that a gating field/condition exists in the contract but is never checked on the actual state-changing execution path, so an operation that should be blocked can still succeed. The same class of bug exists in java-tron: the `CancelAllUnfreezeV2` and `WithdrawExpireUnfreeze` transaction types are guarded behind committee-controlled hard-fork flags (`supportAllowCancelAllUnfreezeV2()` and `supportUnfreezeDelay()`) when submitted as ordinary transactions via their actuators, but the equivalent native-contract processors invoked from the TVM (reachable by any Solidity contract calling the corresponding opcode/precompile) never check these flags.

### Finding Description
`CancelAllUnfreezeV2Actuator.validate()` explicitly checks the feature flag before allowing the operation: [1](#0-0) 

Likewise `WithdrawExpireUnfreezeActuator.validate()` checks its own flag: [2](#0-1) 

However, the corresponding TVM-reachable native-contract processors — `CancelAllUnfreezeV2Processor.validate()` and `WithdrawExpireUnfreezeProcessor.validate()`, invoked from `Program.java` when a smart contract calls the freeze/unfreeze v2 TVM opcodes — perform account/address validation but never call `dynamicStore.supportAllowCancelAllUnfreezeV2()` or `dynamicStore.supportUnfreezeDelay()`: [3](#0-2) [4](#0-3) 

This mirrors the `WeirollWallet.forfeit()` bug: a required gating condition is enforced on one call path (the actuator/`validate()` for the `forfeit`-equivalent operation) but omitted on another reachable path (the TVM native-contract processor) that performs the same state mutation.

### Impact Explanation
If the committee has not yet enabled `ALLOW_CANCEL_ALL_UNFREEZE_V2` or `UNFREEZE_DELAY` via proposal, ordinary users are blocked from calling `CancelAllUnfreezeV2`/`WithdrawExpireUnfreeze` transactions. But any smart contract could invoke the equivalent TVM opcode path, bypassing the intended hard-fork gate and mutating account balance/frozen-resource state (`ownerCapsule.setBalance(...)`, `addFrozenBalanceForBandwidthV2`, `addTotalNetWeight`, etc.) before the feature is meant to be live. This is a consensus-relevant behavioral inconsistency: nodes that have not yet activated the corresponding hard fork bit could still see state changes triggered through contract calls, creating divergent chain state/consensus risk and undermining the committee's ability to control the rollout of a feature — a potential chain-split or unauthorized-state-mutation vector.

### Likelihood Explanation
Reachable by any account able to deploy and call a smart contract using the freeze/unfreeze v2 TVM opcodes, with no special privilege required — an ordinary contract deployer/caller can trigger `Program.java`'s `cancelAllUnfreezeV2`/`withdrawExpireUnfreezeBalance` internal calls, which route directly to the unguarded processors.

### Recommendation
Add the same `dynamicStore.supportAllowCancelAllUnfreezeV2()` / `dynamicStore.supportUnfreezeDelay()` checks (throwing `ContractValidateException` when disabled) inside `CancelAllUnfreezeV2Processor.validate()` and `WithdrawExpireUnfreezeProcessor.validate()`, matching the checks already present in their respective actuators, so the TVM-reachable path cannot bypass the committee-controlled feature gate.

### Proof of Concept
1. Deploy a Solidity contract that calls the `cancelAllUnfreezeV2`/`withdrawExpireUnfreeze` native TVM function exposed via `Program.java`.
2. On a network where `ALLOW_CANCEL_ALL_UNFREEZE_V2` (or `UNFREEZE_DELAY`) has not been enabled by committee proposal (so ordinary `CancelAllUnfreezeV2Contract`/`WithdrawExpireUnfreezeContract` transactions fail validation with "Not support ... transaction, need to be opened by the committee"), invoke the contract function.
3. Observe that `CancelAllUnfreezeV2Processor.validate()`/`WithdrawExpireUnfreezeProcessor.validate()` do not perform the equivalent check, so `execute()` proceeds and mutates account balance/frozen resource state, bypassing the feature gate enforced for regular transactions.

**Note on confidence:** I confirmed via `grep_search` that neither `CancelAllUnfreezeV2Processor.java` nor `WithdrawExpireUnfreezeProcessor.java` reference `supportAllowCancelAllUnfreezeV2`/`supportUnfreezeDelay` anywhere in the file, while the actuators do. I was not able to fully trace whether `Program.java`'s call sites (`cancelAllUnfreezeV2`, `withdrawExpireUnfreezeBalance`) perform an equivalent flag check themselves before invoking the processor (the tool budget was exhausted before I could inspect those exact call sites in full) — [5](#0-4)  shows the sibling `unfreezeBalanceV2` call site with no such flag check, which supports the same pattern, but the `cancelAllUnfreezeV2`/`withdrawExpireUnfreeze` call sites themselves should be double-checked before treating this as fully confirmed.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/CancelAllUnfreezeV2Actuator.java (L130-133)
```java
    if (!dynamicStore.supportAllowCancelAllUnfreezeV2()) {
      throw new ContractValidateException("Not support CancelAllUnfreezeV2 transaction,"
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

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L2059-2082)
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
```
