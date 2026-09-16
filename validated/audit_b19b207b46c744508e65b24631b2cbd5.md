### Title
`WithdrawExpireUnfreezeProcessor` (TVM `WITHDRAWEXPIREUNFREEZE` opcode) skips the `supportUnfreezeDelay` committee-gate that the ordinary `WithdrawExpireUnfreezeActuator` transaction path enforces - ([File: actuator/src/main/java/org/tron/core/vm/nativecontract/WithdrawExpireUnfreezeProcessor.java])

### Summary
The external report flags a class of bug where a reward/withdraw function omits an explicit "has the feature/period actually started" status check that a sibling code path does enforce, letting the operation run in a state it should not be reachable in. The same pattern exists in java-tron between the two implementations of "withdraw expired unfreeze balance": the normal transaction actuator checks a chain parameter before allowing the withdrawal, but the TVM smart-contract opcode implementation of the identical operation does not.

### Finding Description
`WithdrawExpireUnfreezeActuator.validate()` explicitly requires the committee-controlled `supportUnfreezeDelay` chain parameter to be enabled before a `WithdrawExpireUnfreezeContract` transaction is allowed to run: [1](#0-0) 

The TVM native-contract equivalent, `WithdrawExpireUnfreezeProcessor.validate()`, which is invoked directly by any contract executing the `WITHDRAWEXPIREUNFREEZE` opcode, performs address/account/overflow checks but never checks `supportUnfreezeDelay`: [2](#0-1) 

This opcode is reachable from any deployed contract once `VMConfig.allowTvmFreezeV2` (a separate hard-fork/committee flag) is enabled: [3](#0-2) [4](#0-3) 

`allowTvmFreezeV2` and `supportUnfreezeDelay` are distinct, independently toggled chain parameters (as shown by their separate `init*`/getter methods and separate occurrences across `ProposalUtil`, `DynamicPropertiesStore`, `ChainParameterEnum`, and the various Freeze/Unfreeze actuators). Because they are separately controlled, there is a governance window in which `allowTvmFreezeV2` is on (enabling the opcode path) while `supportUnfreezeDelay` is off (meaning the network operators have not yet authorized ordinary accounts to withdraw delayed-unfreeze balances). In that window, the actuator path correctly rejects `WithdrawExpireUnfreezeContract` transactions, but a smart contract calling the `WITHDRAWEXPIREUNFREEZE` opcode bypasses that same governance gate entirely, exactly mirroring the reported pattern of a withdraw/claim method executing "before staking starts" because the code path lacks the explicit status check that its sibling enforces.

### Impact Explanation
This is an inconsistency in enforcement of a committee-controlled feature flag between two code paths that perform the identical state mutation (crediting `unfrozenV2` balances back to `balance`). It allows a contract-based caller to exercise a withdrawal capability that the network operators intentionally have not yet turned on for the general (non-contract) transaction path, undermining the intended governance/rollout control over the `supportUnfreezeDelay` feature. It does not create unbacked balance (the underlying `unfreezeExpireTime <= now` condition still gates whether funds are actually withdrawable), but it does violate the invariant that this whole withdrawal capability is meant to be committee-gated, which the report's own "not critical, but recommend explicit check" characterization matches closely.

### Likelihood Explanation
Reaching it only requires an unprivileged account to deploy/trigger a contract that executes the `WITHDRAWEXPIREUNFREEZE` opcode while `allowTvmFreezeV2` is enabled network-wide; no special privileges are needed. The likelihood of the flag combination (`allowTvmFreezeV2=true`, `supportUnfreezeDelay=false`) actually occurring in practice on mainnet is uncertain and depends on how the java-tron committee/superrepresentatives sequence these two proposal activations — this could not be fully confirmed from the code alone.

### Recommendation
Add the same `supportUnfreezeDelay` (and equivalent) checks to `WithdrawExpireUnfreezeProcessor.validate()` that already exist in `WithdrawExpireUnfreezeActuator.validate()`, so both entry points to the same underlying operation enforce identical governance/status preconditions.

### Proof of Concept
Not independently executable from static analysis alone; the scenario requires a network configuration where `allowTvmFreezeV2` is enabled but `supportUnfreezeDelay` is disabled, and an account with existing `unfrozenV2` entries whose `unfreezeExpireTime` has passed calling the `WITHDRAWEXPIREUNFREEZE` opcode from a contract. I was unable to verify from the index whether such a flag combination is achievable under the current proposal-activation sequencing/hard-fork ordering; a Devin session with full repo/test access would be needed to confirm reachability end-to-end.

### Citations

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

**File:** actuator/src/main/java/org/tron/core/vm/OperationRegistry.java (L615-634)
```java
  public static void appendFreezeV2Operations(JumpTable table) {
    BooleanSupplier proposal = VMConfig::allowTvmFreezeV2;

    table.set(new Operation(
        Op.FREEZEBALANCEV2, 2, 1,
        EnergyCost::getFreezeBalanceV2Cost,
        OperationActions::freezeBalanceV2Action,
        proposal));

    table.set(new Operation(
        Op.UNFREEZEBALANCEV2, 2, 1,
        EnergyCost::getUnfreezeBalanceV2Cost,
        OperationActions::unfreezeBalanceV2Action,
        proposal));

    table.set(new Operation(
        Op.WITHDRAWEXPIREUNFREEZE, 0, 1,
        EnergyCost::getWithdrawExpireUnfreezeCost,
        OperationActions::withdrawExpireUnfreezeAction,
        proposal));
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
