### Title
AccountPermissionUpdateActuator Lets Any Active-Permission Holder With the AccountPermissionUpdateContract Bit Fully Overwrite the Owner (Root) Permission - (File: actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java)

### Summary
`AccountPermissionUpdateContract` is validated only for structural correctness (key count, threshold, distinct addresses, operations bitmap size) and not for who is allowed to rewrite which permission slot. Once any account grants an `Active` permission the `AccountPermissionUpdateContract` operation bit (id 46), the holder of that lower-privileged permission can sign a transaction that completely replaces the account's `Owner` permission with keys of their own choosing — seizing permanent root-level control of the account, exactly mirroring the RustFS `ImportIam` pattern where a limited "import" privilege is used to mint a persistent, attacker-controlled identity under the root parent.

### Finding Description
Transaction-level authorization for a contract is resolved by `permissionId` on the `Contract` message: if `permissionId != 0`, the signer only needs to satisfy an `Active` permission whose `operations` bitmap has the bit for the contract's `ContractType` set [1](#0-0) , verified via `checkPermissionOperations` [2](#0-1) . This is the exact analog of RustFS's `ImportIAMAction` — a scoped, delegated capability.

Once such delegation is granted (the default active-permission operations mask specifically excludes `AccountPermissionUpdateContract`, but nothing prevents an owner from adding it, and other tooling/workflows can construct such permissions), any holder of that `Active` key with sufficient weight can submit an `AccountPermissionUpdateContract` naming their own `permissionId`. `AccountPermissionUpdateActuator.validate()` only checks structural validity of the new `Owner`, `Witness`, and `Active` permissions (`checkPermission`) — it never checks that the caller who is authorized only under an `Active` permission is restricted from replacing the `Owner` permission's keys, nor that the new `Owner` permission must retain any of the original owner's keys [3](#0-2) . `execute()` then unconditionally overwrites all permission slots on the account via `account.updatePermissions(...)` [4](#0-3) , with no post-condition ensuring the true root (`Owner`, `permissionId=0`) key set remains under the legitimate owner's control.

This is structurally identical to the RustFS bug class: a request bearing a scoped, delegated privilege (`ImportIAMAction` / an `Active` permission with the `AccountPermissionUpdateContract` bit) is used to fabricate/overwrite a credential set under an arbitrary "parent" — including the most privileged one (`minioadmin` / the account's `Owner` permission) — because the endpoint/actuator does not enforce that the caller's privilege level bounds what identity it can create or modify.

### Impact Explanation
Successful exploitation permanently locks the legitimate owner out of the account and grants the attacker full root (`Owner`) control, including the ability to reset all further `Owner`/`Witness`/`Active` permissions, transfer all funds via subsequent actuators authorized under the new owner keys, and revoke access for the true owner. This is a concrete unauthorized account takeover leading to theft of funds under Owner control, matching the "unauthorized account operation / theft of funds" acceptance criterion.

### Likelihood Explanation
Exploitation requires that an account previously grant an `Active` permission the `AccountPermissionUpdateContract` operation bit (not the out-of-the-box default), so the precondition is delegation-dependent rather than universally reachable from genesis state — similar to how RustFS's flaw required possession of `ImportIAMAction`. However, any workflow, custodian, or multisig operator that delegates account-management duties to an operational key (a very natural pattern for exchange hot-wallet/service accounts) is exposed, and the actuator provides no additional guardrail once that delegation exists, so a single malicious or compromised operational key immediately yields full root takeover.

### Recommendation
In `AccountPermissionUpdateActuator.validate()`, when the contract is authorized via a non-zero `permissionId` (i.e., an `Active` permission), reject changes to the `Owner` permission (and to `Witness`) unless the transaction is authorized under `permissionId == 0` (the true Owner permission). At minimum, require that any new `Owner` permission retain at least the previously-existing owner key(s)/threshold unless the request itself is signed under the current Owner permission.

### Proof of Concept
1. Account `A` has default `Owner` permission (key `Ka`) and grants an `Active` permission (`id=2`) to key `Kb` with the `AccountPermissionUpdateContract` (id 46) operation bit set (e.g., via an owner-authorized `AccountPermissionUpdateContract`).
2. Attacker in control of `Kb` crafts a new `AccountPermissionUpdateContract` for account `A` with `Contract.permission_id = 2`, setting:
   - `owner` = `Permission{type=Owner, threshold=1, keys=[Kb]}` (removing `Ka` entirely)
   - `actives` = `[Permission{type=Active, threshold=1, keys=[Kb], operations=all-ones}]`
3. Signs the transaction with `Kb` only (satisfies `checkWeight` against the `Active` permission id 2) and broadcasts it.
4. `AccountPermissionUpdateActuator.validate()` passes (only structural checks in `checkPermission` apply) [5](#0-4) ; `execute()` overwrites the account's permissions, making `Kb` the sole Owner and permanently removing `Ka`'s access [6](#0-5) .

### Citations

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

**File:** actuator/src/main/java/org/tron/core/utils/TransactionUtil.java (L171-180)
```java
  public static boolean checkPermissionOperations(Permission permission, Contract contract)
      throws PermissionException {
    ByteString operations = permission.getOperations();
    if (operations.size() != 32) {
      throw new PermissionException("operations size must be 32");
    }
    int contractType = contract.getTypeValue();
    boolean b = (operations.byteAt(contractType / 8) & (1 << (contractType % 8))) != 0;
    return b;
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L34-69)
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

    return true;
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L71-146)
```java
  private boolean checkPermission(Permission permission) throws ContractValidateException {
    DynamicPropertiesStore dynamicStore = chainBaseManager.getDynamicPropertiesStore();
    if (permission.getKeysCount() > dynamicStore.getTotalSignNum()) {
      throw new ContractValidateException("number of keys in permission should not be greater "
          + "than " + dynamicStore.getTotalSignNum());
    }
    if (permission.getKeysCount() == 0) {
      throw new ContractValidateException("key's count should be greater than 0");
    }
    if (permission.getType() == PermissionType.Witness && permission.getKeysCount() != 1) {
      throw new ContractValidateException("Witness permission's key count should be 1");
    }
    if (permission.getThreshold() <= 0) {
      throw new ContractValidateException("permission's threshold should be greater than 0");
    }
    String name = permission.getPermissionName();
    if (!StringUtils.isEmpty(name) && name.length() > 32) {
      throw new ContractValidateException("permission's name is too long");
    }
    //check owner name ?
    if (permission.getParentId() != 0) {
      throw new ContractValidateException("permission's parent should be owner");
    }

    long weightSum = 0;
    List<ByteString> addressList = permission.getKeysList()
        .stream()
        .map(x -> x.getAddress())
        .distinct()
        .collect(toList());
    if (addressList.size() != permission.getKeysList().size()) {
      throw new ContractValidateException(
          "address should be distinct in permission " + permission.getType());
    }
    for (Key key : permission.getKeysList()) {
      if (!DecodeUtil.addressValid(key.getAddress().toByteArray())) {
        throw new ContractValidateException("key is not a validate address");
      }
      if (key.getWeight() <= 0) {
        throw new ContractValidateException("key's weight should be greater than 0");
      }
      try {
        weightSum = addExact(weightSum, key.getWeight());
      } catch (ArithmeticException e) {
        throw new ContractValidateException(e.getMessage());
      }
    }
    if (weightSum < permission.getThreshold()) {
      throw new ContractValidateException(
          "sum of all key's weight should not be less than threshold in permission " + permission
              .getType());
    }

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
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L148-229)
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
  }
```
