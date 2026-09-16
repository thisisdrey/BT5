Based on my investigation, `WitnessAllowanceFrozenTime` (the 24-hour withdrawal cooldown) is only referenced in `WithdrawBalanceActuator` — it is never checked in `WithdrawRewardProcessor`, `VoteRewardUtil`, or the TVM `withdrawReward()` opcode path. This confirms a genuine asymmetry between the two reward-withdrawal entry points.

### Title
Missing 24-hour withdrawal cooldown enforcement in TVM `withdrawReward()` allows unrestricted reward withdrawal frequency, bypassing the `WithdrawBalanceContract` rate limit - (File: actuator/src/main/java/org/tron/core/vm/nativecontract/WithdrawRewardProcessor.java)

### Summary
The legacy `WithdrawBalanceContract` transaction path enforces a mandatory cooldown (`witnessAllowanceFrozenTime`, i.e. 24 hours) between reward withdrawals via `accountCapsule.getLatestWithdrawTime()`. The newer, TVM-reachable `withdrawReward()` native contract path (introduced for `allowTvmVote`) performs the identical allowance-withdraw state mutation but omits this cooldown check entirely, allowing any contract-calling account to withdraw the same `allowance` balance every block instead of once per day, directly analogous to the AmAmm report's finding where a state-transition path fails to re-validate an intended cooldown gate that a sibling code path enforces.

### Finding Description
`WithdrawBalanceActuator.validate()` explicitly gates withdrawal by checking: [1](#0-0) 

This condition compares `now` against `accountCapsule.getLatestWithdrawTime()` plus `dynamicStore.getWitnessAllowanceFrozenTime() * FROZEN_PERIOD` (24 hours in the default config), throwing `ContractValidateException` if the account withdrew too recently.

However, the TVM-exposed `withdrawReward()` native contract, reachable from any smart contract via the `withdrawReward` builtin call in `Program.java`, uses a completely separate validation path: [2](#0-1) 

This `validate()` only rejects Genesis/guard-representative addresses — it never inspects `latestWithdrawTime` or `witnessAllowanceFrozenTime`. The corresponding `execute()` still mutates `allowance` to zero and sets `latestWithdrawTime`, but does so unconditionally on every call: [3](#0-2) 

The entry point into this processor is reachable from an ordinary deployed contract's bytecode: [4](#0-3) 

Both paths ultimately manipulate the same `Account.allowance` / `Account.latest_withdraw_time` fields defined in the protocol: [5](#0-4) 

So an attacker who deploys (or is) a contract holding voting/SR rewards can call `withdrawReward()` from within contract logic on every single block, completely sidestepping the 24-hour throttle that the same operation enforces when submitted as a plain `WithdrawBalanceContract` transaction — mirroring the AmAmm root cause where a specific transition/entry path (`State.D->State.B` there; the TVM native-contract entry here) omits the cooldown check that the "normal" path enforces.

### Impact Explanation
This does not directly create unbacked balance (the amount withdrawn is still bounded by legitimately accrued `allowance`/vote reward), so it is not a fund-creation bug. Its practical effect is bypassing an intended rate-limiting/anti-spam control on reward withdrawal, enabling frequent, unthrottled state writes and reward extraction cadence that the protocol explicitly tries to prevent through `WitnessAllowanceFrozenTime`. This is a medium-severity logic/business-rule bypass consistent with the referenced report's classification (bypassing a designed cooldown limit), rather than a critical fund-theft bug.

### Likelihood Explanation
Likelihood is medium: any account can deploy a trivial contract that calls the `withdrawReward` builtin (exposed as a Solidity precompile-style call gated by `allowTvmVote`), so exploitation requires no special privilege beyond normal contract deployment and having withdrawable `allowance`.

### Recommendation
Add the same cooldown check present in `WithdrawBalanceActuator.validate()` — comparing `now - accountCapsule.getLatestWithdrawTime()` against `dynamicStore.getWitnessAllowanceFrozenTime() * FROZEN_PERIOD` — into `WithdrawRewardProcessor.validate()` before allowing execution, ensuring the TVM-based reward withdrawal path enforces the same throttle as the transaction-based path.

### Proof of Concept
1. Enable `allowTvmVote` and freeze/vote to accrue `allowance` for an account controlled by a smart contract.
2. Deploy a contract that calls the `withdrawReward()` builtin.
3. Call the contract's withdraw function in consecutive blocks (no 24-hour wait).
4. Observe that each call succeeds and resets `allowance` to 0 while updating `latestWithdrawTime`, with no `ContractValidateException` for "less than 24 hours" — contrasting with calling `WithdrawBalanceContract` directly, which would fail under identical timing per `WithdrawBalanceActuatorTest.notTimeToWithdraw`: [6](#0-5)

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/WithdrawBalanceActuator.java (L121-128)
```java
    long latestWithdrawTime = accountCapsule.getLatestWithdrawTime();
    long now = dynamicStore.getLatestBlockHeaderTimestamp();
    long witnessAllowanceFrozenTime = dynamicStore.getWitnessAllowanceFrozenTime() * FROZEN_PERIOD;

    if (now - latestWithdrawTime < witnessAllowanceFrozenTime) {
      throw new ContractValidateException("The last withdraw time is "
          + latestWithdrawTime + ", less than 24 hours");
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/WithdrawRewardProcessor.java (L21-36)
```java
  public void validate(WithdrawRewardParam param, Repository repo) throws ContractValidateException {
    if (repo == null) {
      throw new ContractValidateException(STORE_NOT_EXIST);
    }

    byte[] ownerAddress = param.getOwnerAddress();

    boolean isGP = CommonParameter.getInstance()
        .getGenesisBlock().getWitnesses().stream().anyMatch(witness ->
            Arrays.equals(ownerAddress, witness.getAddress()));
    if (isGP) {
      throw new ContractValidateException(
          ACCOUNT_EXCEPTION_STR + StringUtil.encode58Check(ownerAddress)
              + "] is a guard representative and is not allowed to withdraw Balance");
    }
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/WithdrawRewardProcessor.java (L38-68)
```java
  public long execute(WithdrawRewardParam param, Repository repo) throws ContractExeException {
    byte[] ownerAddress = param.getOwnerAddress();

    VoteRewardUtil.withdrawReward(ownerAddress, repo);

    AccountCapsule accountCapsule = repo.getAccount(ownerAddress);
    long oldBalance = accountCapsule.getBalance();
    long allowance = accountCapsule.getAllowance();
    long newBalance = 0;

    try {
      newBalance = LongMath.checkedAdd(oldBalance, allowance);
    } catch (ArithmeticException e) {
      logger.debug(e.getMessage(), e);
      throw new ContractExeException(e.getMessage());
    }

    // If no allowance, do nothing and just return zero.
    if (allowance <= 0) {
      return 0;
    }

    accountCapsule.setInstance(accountCapsule.getInstance().toBuilder()
        .setBalance(newBalance)
        .setAllowance(0L)
        .setLatestWithdrawTime(param.getNowInMs())
        .build());

    repo.updateAccount(accountCapsule.createDbKey(), accountCapsule);
    return allowance;
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L2340-2369)
```java
  public long withdrawReward() {
    Repository repository = getContractState().newRepositoryChild();
    byte[] owner = getContextAddress();

    increaseNonce();
    InternalTransaction internalTx = addInternalTx(null, owner, owner, 0, null,
        "withdrawReward", nonce, null);

    WithdrawRewardParam param = new WithdrawRewardParam();
    param.setOwnerAddress(owner);
    param.setNowInMs(getTimestamp().longValue() * 1000);
    try {
      WithdrawRewardProcessor processor = new WithdrawRewardProcessor();
      processor.validate(param, repository);
      long allowance = processor.execute(param, repository);
      repository.commit();
      if (internalTx != null) {
        internalTx.setValue(allowance);
      }
      return allowance;
    } catch (ContractValidateException e) {
      logger.warn("TVM WithdrawReward: validate failure. Reason: {}", e.getMessage());
    } catch (ContractExeException e) {
      logger.warn("TVM WithdrawReward: execute failure. Reason: {}", e.getMessage());
    }
    if (internalTx != null) {
      internalTx.reject();
    }
    return 0;
  }
```

**File:** protocol/src/main/protos/core/Tron.proto (L170-173)
```text
  // witness block producing allowance
  int64 allowance = 0x0B;
  // last withdraw time
  int64 latest_withdraw_time = 0x0C;
```

**File:** framework/src/test/java/org/tron/core/actuator/WithdrawBalanceActuatorTest.java (L250-292)
```java
  @Test
  public void notTimeToWithdraw() {
    long now = System.currentTimeMillis();
    dbManager.getDynamicPropertiesStore().saveLatestBlockHeaderTimestamp(now);

    byte[] address = ByteArray.fromHexString(OWNER_ADDRESS);
    try {
      dbManager.getMortgageService()
          .adjustAllowance(dbManager.getAccountStore(), address, allowance);
    } catch (BalanceInsufficientException e) {
      fail("BalanceInsufficientException");
    }
    AccountCapsule accountCapsule = dbManager.getAccountStore().get(address);
    accountCapsule.setLatestWithdrawTime(now);
    Assert.assertEquals(accountCapsule.getAllowance(), allowance);
    Assert.assertEquals(accountCapsule.getLatestWithdrawTime(), now);

    WitnessCapsule witnessCapsule = new WitnessCapsule(ByteString.copyFrom(address), 100,
        "http://baidu.com");

    dbManager.getAccountStore().put(address, accountCapsule);
    dbManager.getWitnessStore().put(address, witnessCapsule);

    WithdrawBalanceActuator actuator = new WithdrawBalanceActuator();
    actuator.setChainBaseManager(dbManager.getChainBaseManager())
        .setAny(getContract(OWNER_ADDRESS));

    TransactionResultCapsule ret = new TransactionResultCapsule();

    try {
      actuator.validate();
      actuator.execute(ret);
      fail("cannot run here.");

    } catch (ContractValidateException e) {
      Assert.assertTrue(e instanceof ContractValidateException);
      Assert
          .assertEquals("The last withdraw time is " + now + ", less than 24 hours",
              e.getMessage());
    } catch (ContractExeException e) {
      Assert.assertFalse(e instanceof ContractExeException);
    }
  }
```
