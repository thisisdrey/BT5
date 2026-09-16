### Title
Active (non-Owner) permission holders can hijack full account control via `AccountPermissionUpdateContract` — ([File: actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java])

### Summary
`AccountPermissionUpdateActuator` treats an `Active` permission that merely has the `AccountPermissionUpdateContract` operation bit set as fully equivalent to the account's `Owner` permission. Any signer who can satisfy the threshold of such an `Active` permission can broadcast an `AccountPermissionUpdateContract` transaction and unilaterally replace the account's `Owner` permission (and every other permission), seizing full administrative control of the account — the same "lesser-permission equals full-admin" misconfiguration class described in the Keycloak advisory (`manage-clients` behaving like `manage-permissions`).

### Finding Description
`TransactionUtil.getTransactionSignWeight` / `TransactionCapsule.checkPermission` / `WalletUtil.checkPermissionOperations` gate which contract a given permission id may authorize purely by checking the 32-byte `operations` bitmap on that permission for the contract's `ContractType` bit — there is no special-case exclusion for `AccountPermissionUpdateContract` itself: [1](#0-0) [2](#0-1) [3](#0-2) 

When an `Active` permission is created/updated, `AccountPermissionUpdateActuator.checkPermission` validates the `operations` bitmap only against the set of globally *available* `ContractType`s — it never forbids setting the bit corresponding to `AccountPermissionUpdateContract`: [4](#0-3) 

Consequently, once an `Active` permission's operations bitmap includes the `AccountPermissionUpdateContract` bit (a state that is either explicitly configured by the owner, or arises because active permissions are commonly created with "all available contract types" enabled for convenience, as exercised by `AccountCapsule.createDefaultActivePermission`/`checkAvailableContractTypeCorrespondingToCode`), any co-signer holding keys in that `Active` permission can sign a transaction with `permissionId` pointing at that Active permission and successfully pass `validate()`: [5](#0-4) 

`execute()` then blindly applies whatever `owner`, `witness`, and `actives` are supplied in the contract — there is no check that the signer's own permission is `Owner`, nor that the new `Owner` permission preserves the original owner's keys: [6](#0-5) 

This lets a party who was only ever intended to co-sign ordinary `Active`-scoped operations (e.g. transfers) instead replace the `Owner` permission's keys entirely with attacker-controlled keys, exactly mirroring the reported Keycloak bug class where a scoped-management permission (`manage-clients`) is silently equivalent to the top-level admin permission (`manage-permissions`).

### Impact Explanation
Successful exploitation grants the attacker exclusive `Owner` control of the victim account: they can permanently lock out the legitimate owner, rewrite all `Active`/`Witness` permissions, and subsequently move/freeze all TRX, TRC10, and TRC20 assets under that account, and control any smart contracts owned by it. This is a concrete unauthorized account takeover leading to theft or permanent freezing of funds.

### Likelihood Explanation
Exploitation requires only that the target account has an `Active` permission whose `operations` bitmap includes the `AccountPermissionUpdateContract` bit and that the attacker controls (or colludes with) enough weight to meet that permission's threshold — a very plausible real-world configuration, since default/broad active-permission templates commonly enable the full contract-type bitmap for convenience rather than restricting it away from account-management operations. No special network position, mining power, or witness/SR status is needed — only a signed transaction from an already-delegated co-signer key.

### Recommendation
In `AccountPermissionUpdateActuator.checkPermission`, explicitly reject `Active` permissions whose `operations` bitmap sets the bit for `ContractType.AccountPermissionUpdateContract` (or otherwise require `permissionId == 0`/`Owner` type to authorize this contract). Additionally, in `AccountPermissionUpdateActuator.execute()`/`validate()`, enforce that only the `Owner` permission (or a signer whose permission id is 0) may submit `AccountPermissionUpdateContract` transactions, since it inherently reconfigures the account's authority structure.

### Proof of Concept
1. Owner account `A` creates/updates its `Active` permission #2 with threshold 1, containing co-signer key `B`, and an `operations` bitmap that includes the bit for `AccountPermissionUpdateContract` (this is common when using an "all contract types" active permission template).
2. Co-signer `B` (holding only what they believe is limited `Active` authority) crafts an `AccountPermissionUpdateContract` transaction for account `A`, setting `permissionId = 2`, and defines a brand-new `Owner` permission containing only `B`'s own key.
3. `B` signs the transaction with their private key; `AccountPermissionUpdateActuator.validate()` passes because permission #2's `operations` bit for `AccountPermissionUpdateContract` is set, and `checkWeight` is satisfied by `B` alone (threshold 1).
4. `AccountPermissionUpdateActuator.execute()` calls `account.updatePermissions(...)` with the attacker-supplied `Owner` permission, replacing `A`'s original owner keys.
5. `B` now solely controls account `A`'s `Owner` permission and can drain or freeze all its assets, while the original owner has been permanently locked out.

### Citations

**File:** actuator/src/main/java/org/tron/core/utils/TransactionUtil.java (L236-244)
```java
        if (permissionId != 0) {
          if (permission.getType() != PermissionType.Active) {
            throw new PermissionException("Permission type is wrong!");
          }
          //check operations
          if (!checkPermissionOperations(permission, contract)) {
            throw new PermissionException("Permission denied!");
          }
        }
```

**File:** chainbase/src/main/java/org/tron/common/utils/WalletUtil.java (L27-37)
```java
  public static boolean checkPermissionOperations(Permission permission, Contract contract)
      throws PermissionException {
    ByteString operations = permission.getOperations();
    if (operations.size() != 32) {
      throw new PermissionException(String.format("operations size must 32, actual: %d",
          operations.size()));
    }
    int contractType = contract.getTypeValue();
    boolean b = (operations.byteAt(contractType / 8) & (1 << (contractType % 8))) != 0;
    return b;
  }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java (L635-645)
```java
  private static void checkPermission(int permissionId, Permission permission, Transaction.Contract contract) throws PermissionException {
    if (permissionId != 0) {
      if (permission.getType() != PermissionType.Active) {
        throw new PermissionException("Permission type is error");
      }
      //check operations
      if (!checkPermissionOperations(permission, contract)) {
        throw new PermissionException("Permission denied");
      }
    }
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L34-66)
```java
  @Override
  public boolean execute(Object object) throws ContractExeException {
    TransactionResultCapsule result = (TransactionResultCapsule) object;
    if (Objects.isNull(result)) {
      throw new RuntimeException(ActuatorConstant.TX_RESULT_NULL);
    }

    AccountStore accountStore = chainBaseManager.getAccountStore();
    long fee = calcFee();
    final AccountPermissionUpdateContract accountPermissionUpdateContract;
    try {
      accountPermissionUpdateContract = any.unpack(AccountPermissionUpdateContract.class);

      byte[] ownerAddress = accountPermissionUpdateContract.getOwnerAddress().toByteArray();
      AccountCapsule account = accountStore.get(ownerAddress);
      account.updatePermissions(accountPermissionUpdateContract.getOwner(),
          accountPermissionUpdateContract.getWitness(),
          accountPermissionUpdateContract.getActivesList());
      accountStore.put(ownerAddress, account);

      adjustBalance(accountStore, ownerAddress, -fee);
      if (chainBaseManager.getDynamicPropertiesStore().supportBlackHoleOptimization()) {
        chainBaseManager.getDynamicPropertiesStore().burnTrx(fee);
      } else {
        adjustBalance(accountStore, accountStore.getBlackhole(), fee);
      }

      result.setStatus(fee, code.SUCESS);
    } catch (BalanceInsufficientException | InvalidProtocolBufferException e) {
      logger.debug(e.getMessage(), e);
      result.setStatus(fee, code.FAILED);
      throw new ContractExeException(e.getMessage());
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L124-145)
```java
    ByteString operations = permission.getOperations();
    if (permission.getType() != PermissionType.Active) {
      if (!operations.isEmpty()) {
        throw new ContractValidateException(
            permission.getType() + " permission needn't operations");
      }
      return true;
    }
    //check operations
    if (operations.isEmpty() || operations.size() != 32) {
      throw new ContractValidateException("operations size must 32");
    }

    byte[] types1 = dynamicStore.getAvailableContractType();
    for (int i = 0; i < 256; i++) {
      boolean b = (operations.byteAt(i / 8) & (1 << (i % 8))) != 0;
      boolean t = ((types1[(i / 8)] & 0xff) & (1 << (i % 8))) != 0;
      if (b && !t) {
        throw new ContractValidateException(i + " isn't a validate ContractType");
      }
    }
    return true;
```

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L148-228)
```java
  @Override
  public boolean validate() throws ContractValidateException {

    if (chainBaseManager == null) {
      throw new ContractValidateException(ActuatorConstant.STORE_NOT_EXIST);
    }

    if (this.any == null) {
      throw new ContractValidateException(ActuatorConstant.CONTRACT_NOT_EXIST);
    }

    AccountStore accountStore = chainBaseManager.getAccountStore();
    DynamicPropertiesStore dynamicStore = chainBaseManager.getDynamicPropertiesStore();

    if (dynamicStore.getAllowMultiSign() != 1) {
      throw new ContractValidateException("multi sign is not allowed, "
          + "need to be opened by the committee");
    }
    if (!this.any.is(AccountPermissionUpdateContract.class)) {
      throw new ContractValidateException(
          "contract type error,expected type [AccountPermissionUpdateContract],real type["
              + any.getClass() + "]");
    }
    final AccountPermissionUpdateContract accountPermissionUpdateContract;
    try {
      accountPermissionUpdateContract = any.unpack(AccountPermissionUpdateContract.class);
    } catch (InvalidProtocolBufferException e) {
      logger.debug(e.getMessage(), e);
      throw new ContractValidateException(e.getMessage());
    }
    byte[] ownerAddress = accountPermissionUpdateContract.getOwnerAddress().toByteArray();
    if (!DecodeUtil.addressValid(ownerAddress)) {
      throw new ContractValidateException("invalidate ownerAddress");
    }
    AccountCapsule accountCapsule = accountStore.get(ownerAddress);
    if (accountCapsule == null) {
      throw new ContractValidateException("ownerAddress account does not exist");
    }

    if (!accountPermissionUpdateContract.hasOwner()) {
      throw new ContractValidateException("owner permission is missed");
    }

    if (accountCapsule.getIsWitness()) {
      if (!accountPermissionUpdateContract.hasWitness()) {
        throw new ContractValidateException("witness permission is missed");
      }
    } else {
      if (accountPermissionUpdateContract.hasWitness()) {
        throw new ContractValidateException("account isn't witness can't set witness permission");
      }
    }

    if (accountPermissionUpdateContract.getActivesCount() == 0) {
      throw new ContractValidateException("active permission is missed");
    }
    if (accountPermissionUpdateContract.getActivesCount() > 8) {
      throw new ContractValidateException("active permission is too many");
    }

    Permission owner = accountPermissionUpdateContract.getOwner();
    Permission witness = accountPermissionUpdateContract.getWitness();
    List<Permission> actives = accountPermissionUpdateContract.getActivesList();

    if (owner.getType() != PermissionType.Owner) {
      throw new ContractValidateException("owner permission type is error");
    }
    checkPermission(owner);
    if (accountCapsule.getIsWitness()) {
      if (witness.getType() != PermissionType.Witness) {
        throw new ContractValidateException("witness permission type is error");
      }
      checkPermission(witness);
    }
    for (Permission permission : actives) {
      if (permission.getType() != PermissionType.Active) {
        throw new ContractValidateException("active permission type is error");
      }
      checkPermission(permission);
    }
    return true;
```
