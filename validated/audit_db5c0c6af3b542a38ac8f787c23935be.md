### Title
Zero-cost, permanent squatting of the global unique `account_id` namespace via `SetAccountIdContract` - (File: `actuator/src/main/java/org/tron/core/actuator/SetAccountIdActuator.java`)

### Summary
`SetAccountIdActuator` lets any existing account permanently register a globally-unique, case-insensitive `account_id` for zero fee. Because the check-then-set is a simple uniqueness lookup with no cost or auction mechanism, any address can pre-emptively claim desirable IDs at scale and later resell/withhold them, exactly the "name squatting" pattern described in the source report for Lens Protocol profile handles.

### Finding Description
`SetAccountIdActuator.validate()` only checks that the account exists, has no ID set yet, and that the requested `accountId` is not already taken in `accountIdIndexStore`: [1](#0-0) 
`calcFee()` unconditionally returns `0`, meaning claiming an ID costs nothing beyond ordinary bandwidth/energy for the transaction itself: [2](#0-1) 
Once set, the ID is immutable for that account (`account.getAccountId()` non-empty blocks re-setting), and the `account_id` field is documented as "unique and case insensitive" — i.e., a global human-readable handle namespace analogous to Lens's profile handles: [3](#0-2) 
Because account creation itself is cheap (a fixed `AccountCreateContract` fee, not an auction), an attacker can create many accounts and, for each, submit a free `SetAccountIdContract` to reserve any desirable ID string (brand names, exchange names, short/premium handles) before legitimate users can, then transfer/sell the underlying account to monetize the squatted ID.

### Impact Explanation
This mirrors the reported bug class: a globally-unique naming resource is claimable at effectively zero marginal cost with no auction, deposit, or slashing mechanism, enabling systematic squatting that degrades the usability/value of the identifier namespace for legitimate users and creates a monetizable extraction vector for attackers. It does not directly cause fund loss, but it is a concrete, permanently-effective denial of a shared on-chain resource (the `account_id` namespace) reachable by any ordinary signed transaction.

### Likelihood Explanation
Likelihood is high: no privileged role is required, the transaction is a single low-cost `SetAccountIdContract` call reachable by any account holder, and the actuator's validation path performs no economic deterrent (fee is hardcoded to `0`).

### Recommendation
Introduce a non-zero, non-refundable fee (or auction/bid mechanism) in `SetAccountIdActuator.calcFee()` proportional to demand, and/or require a minimum staking/holding period before an `account_id` can be claimed, to raise the cost of squatting the unique identifier namespace, similar to the mitigation recommended in the original report (auctioning or fee-based allocation of scarce names).

### Proof of Concept
1. Attacker creates N accounts via `AccountCreateContract` (cheap, fixed fee).
2. For each account, attacker submits `SetAccountIdContract` with a desirable `accountId` string; `SetAccountIdActuator.validate()`/`execute()` accept it as long as the ID is unused, and `calcFee()` charges `0`: [4](#0-3) 
3. Attacker now permanently owns many premium `account_id` handles and can transfer/sell the corresponding accounts, with no cost incurred beyond standard bandwidth/energy for the setup transactions.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/SetAccountIdActuator.java (L26-54)
```java
  @Override
  public boolean execute(Object result) throws ContractExeException {
    TransactionResultCapsule ret = (TransactionResultCapsule) result;
    if (Objects.isNull(ret)) {
      throw new RuntimeException(ActuatorConstant.TX_RESULT_NULL);
    }

    final SetAccountIdContract setAccountIdContract;
    final long fee = calcFee();
    AccountStore accountStore = chainBaseManager.getAccountStore();
    AccountIdIndexStore accountIdIndexStore = chainBaseManager.getAccountIdIndexStore();
    try {
      setAccountIdContract = any.unpack(SetAccountIdContract.class);
    } catch (InvalidProtocolBufferException e) {
      logger.debug(e.getMessage(), e);
      ret.setStatus(fee, code.FAILED);
      throw new ContractExeException(e.getMessage());
    }

    byte[] ownerAddress = setAccountIdContract.getOwnerAddress().toByteArray();
    AccountCapsule account = accountStore.get(ownerAddress);

    account.setAccountId(setAccountIdContract.getAccountId().toByteArray());
    accountStore.put(ownerAddress, account);
    accountIdIndexStore.put(account);
    ret.setStatus(fee, code.SUCESS);

    return true;
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/SetAccountIdActuator.java (L86-96)
```java

    AccountCapsule account = accountStore.get(ownerAddress);
    if (account == null) {
      throw new ContractValidateException("Account has not existed");
    }
    if (account.getAccountId() != null && !account.getAccountId().isEmpty()) {
      throw new ContractValidateException("This account id already set");
    }
    if (accountIdIndexStore.has(accountId)) {
      throw new ContractValidateException("This id has existed");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/SetAccountIdActuator.java (L106-109)
```java
  @Override
  public long calcFee() {
    return 0;
  }
```

**File:** protocol/src/main/protos/core/contract/account_contract.proto (L38-41)
```text
// Set account id if the account has no id. Account id is unique and case insensitive.
message SetAccountIdContract {
  bytes account_id = 1;
  bytes owner_address = 2;
```
