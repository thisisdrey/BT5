Based on my investigation, I found two related actuator/native-contract pairs that implement the same withdraw-expired-unfreeze logic in two independent code paths — analogous to the reported bug class of two components (a transaction-level actuator and a TVM precompiled/native path) that are meant to be functionally equivalent but diverge, creating a fund-accounting risk when one path is out of sync with the other.

### Title
Duplicate, independently-maintained "expire unfreeze" balance-crediting logic between `WithdrawExpireUnfreezeActuator` and `WithdrawExpireUnfreezeProcessor` risks fund-accounting divergence - (File: actuator/src/main/java/org/tron/core/actuator/WithdrawExpireUnfreezeActuator.java)

### Summary
`WithdrawExpireUnfreezeActuator` (reachable from a signed `WithdrawExpireUnfreezeContract` transaction) and `WithdrawExpireUnfreezeProcessor` (reachable from the TVM `withdrawExpireUnfreeze()` native contract call in `Program.java`) both independently implement the exact same "release matured unfrozen TRX into balance" logic, each with its own copy of `getTotalWithdrawUnfreeze`/`getRemainWithdrawList`/validate checks. [1](#0-0) [2](#0-1) 

### Finding Description
Both classes read `account.getUnfrozenV2List()`, sum matured entries (`unfreezeExpireTime <= now`), add that sum to `balance`, then rewrite the list with only the still-locked entries. They are two separate implementations of one invariant ("credit exactly the matured, not-yet-withdrawn amount, exactly once"), maintained in two files with slightly different guard conditions: the actuator's `validate()` throws when `totalWithdrawUnfreeze <= 0` (line 110 of `WithdrawExpireUnfreezeActuator.java`) while the processor's `validate()` only throws when `totalWithdrawUnfreeze < 0` (line 47 of `WithdrawExpireUnfreezeProcessor.java`), i.e., the TVM path treats a zero-amount withdraw as valid while the transaction-level path rejects it. This is the same class of bug as the external report: two "compatible" components that are supposed to reconcile the same balance state via a shared invariant but were implemented/maintained independently and have already drifted in their boundary-condition semantics. [3](#0-2) [4](#0-3) 

### Impact Explanation
Currently the observed divergence (validate `< 0` vs `<= 0`) is only a minor behavioral inconsistency (the TVM path silently succeeds with a zero-value internal transaction instead of reverting), not by itself a fund-theft bug. However, because the balance-crediting arithmetic is duplicated instead of shared, any future change to one implementation (e.g., a new resource type, a new unfreeze-delay rule, or a bug fix) that is not mirrored in the other reintroduces the exact "provider/vault incompatibility" pattern from the report: one path could compute or credit a different amount than the other for what should be the same state transition, allowing double-crediting or balance desynchronization between the actuator-driven and TVM-driven withdrawal paths for the same account.

### Likelihood Explanation
Low likelihood of an immediate exploit today since the current logic is arithmetically equivalent aside from the boundary case; but the likelihood of a silent regression is elevated because this invariant is implemented twice, with no shared abstraction, no cross-checks, and no unit test asserting output-equivalence between `WithdrawExpireUnfreezeActuator.execute()` and `WithdrawExpireUnfreezeProcessor.execute()`. Any single unfrozen contract call or JSON-RPC/HTTP-triggered smart contract invoking `withdrawExpireUnfreeze()` (Program.java opcode `WITHDRAWEXPIREUNFREEZE`) exercises the divergent path. [5](#0-4) 

### Recommendation
Consolidate the withdraw-expire-unfreeze balance computation (`getTotalWithdrawUnfreeze`, `getRemainWithdrawList`, and the `<=0`/`<0` boundary check) into a single shared implementation used by both `WithdrawExpireUnfreezeActuator` and `WithdrawExpireUnfreezeProcessor`, and add a test asserting both entry points produce identical account state for identical inputs.

### Proof of Concept
1. Freeze TRX via `freezeBalanceV2`, then `unfreezeBalanceV2` to place an entry in `unfrozenV2List` with `unfreezeExpireTime == now` (zero-remaining-time edge case).
2. Call the TVM native `withdrawExpireUnfreeze()` opcode via a deployed contract when `totalWithdrawUnfreeze == 0` — `WithdrawExpireUnfreezeProcessor.validate` (line 47, `< 0` check) allows this to proceed and commit, whereas the equivalent `WithdrawExpireUnfreezeContract` transaction via `WithdrawExpireUnfreezeActuator.validate` (line 110, `<= 0` check) would reject the same state with `ContractValidateException`, demonstrating the two "compatible" paths diverge for identical account state.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/WithdrawExpireUnfreezeActuator.java (L34-66)
```java
  @Override
  public boolean execute(Object result) throws ContractExeException {
    TransactionResultCapsule ret = (TransactionResultCapsule) result;
    if (Objects.isNull(ret)) {
      throw new RuntimeException(ActuatorConstant.TX_RESULT_NULL);
    }
    long fee = calcFee();
    AccountStore accountStore = chainBaseManager.getAccountStore();
    DynamicPropertiesStore dynamicStore = chainBaseManager.getDynamicPropertiesStore();
    final WithdrawExpireUnfreezeContract withdrawExpireUnfreezeContract;
    try {
      withdrawExpireUnfreezeContract = any.unpack(WithdrawExpireUnfreezeContract.class);
    } catch (InvalidProtocolBufferException e) {
      logger.debug(e.getMessage(), e);
      ret.setStatus(fee, code.FAILED);
      throw new ContractExeException(e.getMessage());
    }
    AccountCapsule accountCapsule = accountStore.get(
        withdrawExpireUnfreezeContract.getOwnerAddress().toByteArray());
    long now = dynamicStore.getLatestBlockHeaderTimestamp();
    List<UnFreezeV2> unfrozenV2List = accountCapsule.getInstance().getUnfrozenV2List();
    long totalWithdrawUnfreeze = getTotalWithdrawUnfreeze(unfrozenV2List, now);
    accountCapsule.setInstance(accountCapsule.getInstance().toBuilder()
        .setBalance(accountCapsule.getBalance() + totalWithdrawUnfreeze)
        .build());
    List<UnFreezeV2> newUnFreezeList = getRemainWithdrawList(unfrozenV2List, now);
    accountCapsule.clearUnfrozenV2();
    accountCapsule.addAllUnfrozenV2(newUnFreezeList);
    accountStore.put(accountCapsule.createDbKey(), accountCapsule);
    ret.setWithdrawExpireAmount(totalWithdrawUnfreeze);
    ret.setStatus(fee, code.SUCESS);
    return true;
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/WithdrawExpireUnfreezeActuator.java (L106-120)
```java

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

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/WithdrawExpireUnfreezeProcessor.java (L43-56)
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

```

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/WithdrawExpireUnfreezeProcessor.java (L72-90)
```java
  public long execute(WithdrawExpireUnfreezeParam param, Repository repo) throws ContractExeException {
    byte[] ownerAddress = param.getOwnerAddress();
    DynamicPropertiesStore dynamicStore = repo.getDynamicPropertiesStore();
    AccountCapsule ownerCapsule = repo.getAccount(ownerAddress);
    long now = dynamicStore.getLatestBlockHeaderTimestamp();
    List<Protocol.Account.UnFreezeV2> unfrozenV2List = ownerCapsule.getInstance().getUnfrozenV2List();
    long totalWithdrawUnfreeze = getTotalWithdrawUnfreeze(unfrozenV2List, now);
    if (totalWithdrawUnfreeze <= 0) {
      return 0;
    }
    ownerCapsule.setInstance(ownerCapsule.getInstance().toBuilder()
        .setBalance(ownerCapsule.getBalance() + totalWithdrawUnfreeze)
        .build());
    List<Protocol.Account.UnFreezeV2> newUnFreezeList = getRemainWithdrawList(unfrozenV2List, now);
    ownerCapsule.clearUnfrozenV2();
    ownerCapsule.addAllUnfrozenV2(newUnFreezeList);
    repo.updateAccount(ownerCapsule.createDbKey(), ownerCapsule);
    return totalWithdrawUnfreeze;
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
