### Title
Witness allowance withdrawal cooldown can be bypassed via the TVM `WITHDRAWREWARD` opcode - (File: `actuator/src/main/java/org/tron/core/vm/nativecontract/WithdrawRewardProcessor.java`)

### Summary
The 24-hour withdrawal cooldown that `WithdrawBalanceActuator` enforces for withdrawing a witness's block-production allowance is not enforced by the equivalent TVM native-contract path (`WithdrawRewardProcessor`, invoked by the `WITHDRAWREWARD` opcode). This mirrors the reported bug class: two code paths performing conceptually the same state transition on the same fields enforce different (contradictory) time-based invariants, letting an actor bypass the intended restriction through the alternate path.

### Finding Description
`WithdrawBalanceActuator.validate()` enforces that a witness may only withdraw allowance once every `FROZEN_PERIOD` (24 hours), computed from `accountCapsule.getLatestWithdrawTime()`: [1](#0-0) 

This check exists specifically to throttle repeated withdrawals of the witness `allowance` field, which is then merged into `balance` and `latestWithdrawTime` is updated: [2](#0-1) 

However, the same conceptual operation (moving `allowance` into `balance` and updating `latestWithdrawTime`) is also implemented as a TVM native contract reachable via the `WITHDRAWREWARD` opcode, through `Program.withdrawReward()`: [3](#0-2) 

`WithdrawRewardProcessor.validate()` only rejects genesis-representative accounts and performs *no* time-based check at all before `execute()` unconditionally zeroes `allowance`, adds it to `balance`, and sets `latestWithdrawTime`: [4](#0-3) 

The opcode is registered and gated only by the `allowTvmVote` feature flag, not by the cooldown: [5](#0-4) 

Since a smart contract can be deployed and triggered by any account, and `withdrawReward()` operates on `getContextAddress()` (the calling contract's own address), a witness account acting through/as a contract can call `WITHDRAWREWARD` repeatedly, each time draining any accrued `allowance` into `balance` immediately, without waiting the 24-hour period that the classical `WithdrawBalanceContract` path enforces.

### Impact Explanation
This breaks the intended withdrawal-rate invariant for witness allowance funds. While it does not directly create unbacked balance (the allowance amount withdrawn is capped to what has actually accrued), it defeats a protocol-level restriction that governs fund movement, analogous to the reported "withdraw funds can start before round has ended" issue where one enforcement path's time gate is silently absent/weaker in a parallel path performing the same operation.

### Likelihood Explanation
Any witness account can trivially deploy a minimal contract that calls the `withdrawreward()` TVM builtin (as already exercised in `VoteTest.java`) and invoke it as often as desired, with `allowTvmVote` enabled (a standard, already-activated feature in current networks). No special privilege beyond being a witness with accrued allowance is required.

### Recommendation
Add the same `latestWithdrawTime` / `FROZEN_PERIOD` cooldown check present in `WithdrawBalanceActuator.validate()` to `WithdrawRewardProcessor.validate()`, so both paths that withdraw the witness allowance enforce an identical, consistent time restriction.

### Proof of Concept
1. A witness account accrues `allowance` via block/transaction-fee rewards (`MortgageService.payReward`).
2. Instead of broadcasting a `WithdrawBalanceContract` transaction (which would be throttled by the 24-hour check in `WithdrawBalanceActuator.validate()`), the witness deploys/uses a contract exposing a method that calls the `withdrawreward()` TVM builtin (as in `VoteTest.CODE`).
3. The witness repeatedly triggers this contract method; each call reaches `Program.withdrawReward()` → `WithdrawRewardProcessor.validate()`/`execute()`, which performs no cooldown check and immediately moves any newly accrued `allowance` into `balance`, bypassing the intended 24-hour withdrawal restriction enforced elsewhere in the protocol.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/WithdrawBalanceActuator.java (L54-68)
```java
    mortgageService.withdrawReward(withdrawBalanceContract.getOwnerAddress()
        .toByteArray());

    AccountCapsule accountCapsule = accountStore.
        get(withdrawBalanceContract.getOwnerAddress().toByteArray());
    long oldBalance = accountCapsule.getBalance();
    long allowance = accountCapsule.getAllowance();

    long now = dynamicStore.getLatestBlockHeaderTimestamp();
    accountCapsule.setInstance(accountCapsule.getInstance().toBuilder()
        .setBalance(oldBalance + allowance)
        .setAllowance(0L)
        .setLatestWithdrawTime(now)
        .build());
    accountStore.put(accountCapsule.createDbKey(), accountCapsule);
```

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

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/WithdrawRewardProcessor.java (L21-68)
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

**File:** actuator/src/main/java/org/tron/core/vm/OperationRegistry.java (L593-602)
```java
  public static void appendVoteOperations(JumpTable table) {
    BooleanSupplier proposal = VMConfig::allowTvmVote;

    table.set(DEFAULT_VOTEWITNESS);

    table.set(new Operation(
        Op.WITHDRAWREWARD, 0, 1,
        EnergyCost::getWithdrawRewardCost,
        OperationActions::withdrawRewardAction,
        proposal));
```
