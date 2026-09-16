### Title
Missing `type` validation in `CreateAccountActuator` lets any funded account create arbitrary addresses with attacker-chosen `AccountType` (including fake `Contract`), permanently blocking normal TRX/TRC10 transfers to those addresses - (File: `actuator/src/main/java/org/tron/core/actuator/CreateAccountActuator.java`)

### Summary
`AccountCreateContract` carries an attacker-controlled `type` field (`AccountType`: `Normal`/`AssetIssue`/`Contract`) alongside an arbitrary, attacker-chosen `account_address` that need not belong to the signer. `CreateAccountActuator.validate()` never checks this `type` field — the check is literally commented out — so any account holder able to pay the (tiny) creation fee can pre-create any not-yet-existing address as `AccountType.Contract`, even though no smart contract bytecode was ever deployed there.

### Finding Description
`AccountCreateContract` allows the signer (`owner_address`) to create an account at a completely different `account_address` chosen by the signer, with a `type` field set arbitrarily: [1](#0-0) 

`CreateAccountActuator.execute()` builds the new `AccountCapsule` directly from the unpacked contract, storing `contract.getType()`/`getTypeValue()` verbatim into the new account: [2](#0-1) 

`AccountCapsule`'s constructor from `AccountCreateContract` simply copies `contract.getType()`/`getTypeValue()` into the stored `Account` proto without any restriction to `Normal`: [3](#0-2) 

`CreateAccountActuator.validate()` checks the owner's balance, address validity, and whether the target address already exists — but the block that would validate `contract.getType()` is explicitly commented out, meaning it performs **no validation at all** on the requested account type: [4](#0-3) 

Downstream code trusts `AccountCapsule.getType() == AccountType.Contract` as a reliable signal that an address hosts a real deployed smart contract, and uses it to *block* ordinary value transfers to that address once the `ForbidTransferToContract` chain parameter is enabled (this parameter is a standard, frequently-enabled governance setting): [5](#0-4) [6](#0-5) 

The same fake `Contract` type is also used elsewhere to reject resource delegation to the address: [7](#0-6) 

Because `CreateAccountActuator.validate()` rejects creation once `accountStore.has(accountAddress)` is true, once an attacker pre-creates a target address with a bogus `type=Contract`, that account's type field can never be reset to `Normal` by the real intended owner through this actuator — `AccountUpdateContract` only changes the account name, not the type: [8](#0-7) 

### Impact Explanation
An unprivileged, minimally-funded transaction broadcaster can:
1. Compute or predict an address that a victim intends to use in the future (e.g., a freshly generated but not-yet-activated wallet address, or an address about to be funded by an exchange/service).
2. Broadcast a single `AccountCreateContract` transaction naming that address as `account_address` and setting `type = Contract`.
3. Permanently mark that address as `AccountType.Contract` in on-chain state, even though it holds no bytecode.

Once mainnet governance enables `FORBID_TRANSFER_TO_CONTRACT` (a real, already-used chain parameter), every subsequent `TransferContract`/`TransferAssetContract` sent to that address is rejected with "Cannot transfer TRX/asset to a smartContract," and resource delegation to it is also blocked. This is a permanent denial-of-service / freezing of a victim's ability to *receive* normal transfers at that address, achievable cheaply and repeatably by any account able to pay the account-creation fee — matching the CVE's bug class of unauthenticated/low-privilege creation of objects with attacker-chosen identity data that produces unauthorized, persistent state changes.

### Likelihood Explanation
Likelihood is high for the mechanics (any funded account can invoke `AccountCreateContract` with a chosen `type` and `account_address`; there is no signature/permission tie between `account_address` and the signer's key, and the validation gap is explicit/commented-out in the source). The full "receive funds blocked" impact additionally depends on `FORBID_TRANSFER_TO_CONTRACT` being enabled on the target network, which is a normal governance-set parameter and not itself a security control.

### Recommendation
- In `CreateAccountActuator.validate()`, restrict `contract.getType()` to `AccountType.Normal` (or otherwise verify that `Contract`/`AssetIssue` types can only be set through the actuators that legitimately create such accounts — `VMActuator.create()` for contracts, `AssetIssueActuator` for asset-issue accounts), rejecting any other externally supplied type in `AccountCreateContract`.
- Alternatively/additionally, make the "is this address a real contract" check in `TransferActuator`/`TransferAssetActuator`/`FreezeBalanceActuator` authoritative by checking `ContractStore.has(address)` rather than trusting `AccountCapsule.getType()`.

### Proof of Concept
1. Attacker account `A` holds enough TRX to pay the account-creation fee.
2. Attacker crafts and signs an `AccountCreateContract` with `owner_address = A`, `account_address = V` (victim's not-yet-existing address), `type = Contract`, and broadcasts it.
3. `CreateAccountActuator.validate()` passes (no type check; `V` does not yet exist), and `execute()` stores an `AccountCapsule` for `V` with `AccountType.Contract`. [9](#0-8) 
4. With `FORBID_TRANSFER_TO_CONTRACT = 1`, any later `TransferContract` sent to `V` fails validation with "Cannot transfer TRX to a smartContract," permanently denying `V`'s owner the ability to receive TRX via normal transfer once they gain control of the address. [5](#0-4)

### Citations

**File:** protocol/src/main/protos/core/contract/account_contract.proto (L26-30)
```text
message AccountCreateContract {
  bytes owner_address = 1;
  bytes account_address = 2;
  AccountType type = 3;
}
```

**File:** actuator/src/main/java/org/tron/core/actuator/CreateAccountActuator.java (L29-65)
```java
  @Override
  public boolean execute(Object result)
      throws ContractExeException {
    TransactionResultCapsule ret = (TransactionResultCapsule) result;
    if (Objects.isNull(ret)) {
      throw new RuntimeException(ActuatorConstant.TX_RESULT_NULL);
    }

    long fee = calcFee();
    DynamicPropertiesStore dynamicStore = chainBaseManager.getDynamicPropertiesStore();
    AccountStore accountStore = chainBaseManager.getAccountStore();
    try {
      AccountCreateContract accountCreateContract = any.unpack(AccountCreateContract.class);
      boolean withDefaultPermission =
          dynamicStore.getAllowMultiSign() == 1;
      AccountCapsule accountCapsule = new AccountCapsule(accountCreateContract,
          dynamicStore.getLatestBlockHeaderTimestamp(), withDefaultPermission, dynamicStore);

      accountStore
          .put(accountCreateContract.getAccountAddress().toByteArray(), accountCapsule);

      adjustBalance(accountStore, accountCreateContract.getOwnerAddress().toByteArray(), -fee);
      // Add to blackhole address
      if (dynamicStore.supportBlackHoleOptimization()) {
        dynamicStore.burnTrx(fee);
      } else {
        adjustBalance(accountStore, accountStore.getBlackhole(), fee);
      }
      ret.setStatus(fee, code.SUCESS);
    } catch (BalanceInsufficientException | InvalidProtocolBufferException e) {
      logger.debug(e.getMessage(), e);
      ret.setStatus(fee, code.FAILED);
      throw new ContractExeException(e.getMessage());
    }

    return true;
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/CreateAccountActuator.java (L91-121)
```java
    byte[] ownerAddress = contract.getOwnerAddress().toByteArray();
    if (!DecodeUtil.addressValid(ownerAddress)) {
      throw new ContractValidateException("Invalid ownerAddress");
    }

    AccountCapsule accountCapsule = accountStore.get(ownerAddress);
    if (accountCapsule == null) {
      String readableOwnerAddress = StringUtil.createReadableString(ownerAddress);
      throw new ContractValidateException(
          ActuatorConstant.ACCOUNT_EXCEPTION_STR
              + readableOwnerAddress + NOT_EXIST_STR);
    }

    final long fee = calcFee();
    if (accountCapsule.getBalance() < fee) {
      throw new ContractValidateException(
          "Validate CreateAccountActuator error, insufficient fee.");
    }

    byte[] accountAddress = contract.getAccountAddress().toByteArray();
    if (!DecodeUtil.addressValid(accountAddress)) {
      throw new ContractValidateException("Invalid account address");
    }

//    if (contract.getType() == null) {
//      throw new ContractValidateException("Type is null");
//    }

    if (accountStore.has(accountAddress)) {
      throw new ContractValidateException("Account has existed");
    }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/AccountCapsule.java (L99-122)
```java
  public AccountCapsule(final AccountCreateContract contract, long createTime,
      boolean withDefaultPermission, DynamicPropertiesStore dynamicPropertiesStore) {
    if (withDefaultPermission) {
      Permission owner = createDefaultOwnerPermission(contract.getAccountAddress());
      Permission active = createDefaultActivePermission(contract.getAccountAddress(),
          dynamicPropertiesStore);

      this.account = Account.newBuilder()
          .setType(contract.getType())
          .setAddress(contract.getAccountAddress())
          .setTypeValue(contract.getTypeValue())
          .setCreateTime(createTime)
          .setOwnerPermission(owner)
          .addActivePermission(active)
          .build();
    } else {
      this.account = Account.newBuilder()
          .setType(contract.getType())
          .setAddress(contract.getAccountAddress())
          .setTypeValue(contract.getTypeValue())
          .setCreateTime(createTime)
          .build();
    }
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/TransferActuator.java (L132-139)
```java
      //after ForbidTransferToContract proposal, send trx to smartContract by actuator is not allowed.
      if (dynamicStore.getForbidTransferToContract() == 1
          && toAccount != null
          && toAccount.getType() == AccountType.Contract) {

        throw new ContractValidateException("Cannot transfer TRX to a smartContract.");

      }
```

**File:** actuator/src/main/java/org/tron/core/actuator/TransferAssetActuator.java (L169-175)
```java
    AccountCapsule toAccount = accountStore.get(toAddress);
    if (toAccount != null) {
      //after ForbidTransferToContract proposal, send trx to smartContract by actuator is not allowed.
      if (dynamicStore.getForbidTransferToContract() == 1
          && toAccount.getType() == AccountType.Contract) {
        throw new ContractValidateException("Cannot transfer asset to smartContract.");
      }
```

**File:** actuator/src/main/java/org/tron/core/actuator/FreezeBalanceActuator.java (L262-267)
```java
      if (dynamicStore.getAllowTvmConstantinople() == 1
          && receiverCapsule.getType() == AccountType.Contract) {
        throw new ContractValidateException(
            "Do not allow delegate resources to contract addresses");

      }
```

**File:** actuator/src/main/java/org/tron/core/actuator/UpdateAccountActuator.java (L42-47)
```java
    byte[] ownerAddress = accountUpdateContract.getOwnerAddress().toByteArray();
    AccountCapsule account = chainBaseManager.getAccountStore().get(ownerAddress);

    account.setAccountName(accountUpdateContract.getAccountName().toByteArray());
    chainBaseManager.getAccountStore().put(ownerAddress, account);
    chainBaseManager.getAccountIndexStore().put(account);
```
