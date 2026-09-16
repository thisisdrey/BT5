### Title
Privilege Escalation via Scoped Active-Permission Signer Rewriting Owner/Witness Permissions - (File: `actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java`)

### Summary
`AccountPermissionUpdateActuator` allows any signer authorized through a non-owner (`Active`) permission id to submit an `AccountPermissionUpdateContract` and completely overwrite the account's `Owner`, `Witness`, and all `Active` permissions, as long as that `Active` permission's 32-byte operations bitmap happens to include the bit for `AccountPermissionUpdateContract` (contract type 46). Nothing in `validate()` or `execute()` restricts the *scope* of the new permission set to be no broader than the permission that authorized the transaction. This mirrors the Budibase bug class: a narrowly-scoped role (an "app-scoped builder" / here a scoped `Active` co-signer) is able to use an update endpoint to grant itself broader/unrelated privileges (full `Owner` control) because the endpoint fails to validate that the assigned privileges stay within the caller's own scope.

### Finding Description
Transaction-level authorization for a contract signed under a non-zero `permissionId` only requires that the permission be of type `Active` and that the contract type bit be set in that permission's `operations` bitmap: [1](#0-0) [2](#0-1) 

If the account owner (or a compromised/malicious co-owner) has created an `Active` permission whose 256-bit operations mask includes bit 46 (`AccountPermissionUpdateContract` — confirmed enabled/available by default via `getAvailableContractType`, as shown in the test `checkAvailableContractTypeCorrespondingToCode` which includes every contract type except `ClearABIContract`/`UpdateBrokerageContract`), then any key holder in that `Active` permission can sign and broadcast an `AccountPermissionUpdateContract` for that account.

`AccountPermissionUpdateActuator.validate()` only checks the *structural* validity of the new `owner`/`witness`/`actives` permissions (key counts, thresholds, distinct addresses, operations bitmap validity) — it never checks who authorized the transaction (`contract.getPermissionId()`) or restricts the new `Owner`/`Witness` permission definitions to be a subset of, or otherwise bounded by, the `Active` permission that was used to sign: [3](#0-2) 

`execute()` then unconditionally overwrites all permissions on the account with attacker-supplied data: [4](#0-3) 

As a result, a signer who was only meant to hold a narrow, delegated `Active` capability (e.g., co-signing routine transfers) — but whose operations mask also happens to cover `AccountPermissionUpdateContract` — can unilaterally replace the account's `Owner` permission with one that gives themselves sufficient weight to meet the owner threshold, remove the original owner's key entirely, or grant themselves any other `Active`/`Witness` permission on the account. This is a direct, unauthorized-account-operation privilege escalation reachable from a single signed transaction submitted through the public broadcast/HTTP/gRPC surface (`AccountPermissionUpdateServlet`, `Wallet.broadcastTransaction`), analogous to Budibase's builder-role scope bypass in its user update API.

### Impact Explanation
An attacker who controls (or compromises) one key of a limited, delegated `Active` permission that has `AccountPermissionUpdateContract` enabled in its operations mask can seize full `Owner` control of the victim account without ever holding an `Owner`-level signature. Once `Owner`, the attacker can transfer out all TRX/TRC10/frozen-resource balances, revert other co-signers' access, or set an arbitrary witness permission if the account is a witness — a concrete unauthorized account takeover and theft-of-funds scenario, matching the "unauthorized account operation / theft of funds" bar required for validity.

### Likelihood Explanation
Exploitation requires only that a victim account previously grant an `Active` permission whose operations bitmap includes bit 46. Because `AccountPermissionUpdateContract` is part of the *default* "all contract types enabled" bitmap used by wallets/tools when constructing broad `Active` permissions (as shown by the default bitmask in `checkAvailableContractTypeCorrespondingToCode`), it is easy for account owners — especially those using generic multisig setups or third-party custody/DeFi tooling — to unintentionally grant a co-signer or automation key this capability while believing it only covers unrelated operations. No special network position or unusual conditions are needed; a single crafted, signed `AccountPermissionUpdateContract` transaction broadcast through the normal HTTP/gRPC path is sufficient.

### Recommendation
In `AccountPermissionUpdateActuator.validate()`, cross-check `contract.getPermissionId()` against the new permission set being installed:
- Require that transactions authorized by a non-owner (`Active`) permission id cannot modify the `Owner` permission, the `Witness` permission, or any `Active` permission other than the one that authorized the call.
- Alternatively, exclude `AccountPermissionUpdateContract` (and any other permission-management contract types) from the set of contract types that can ever be enabled in a delegated `Active` permission's operations bitmap, forcing all permission changes to require `Owner`-level (`permissionId == 0`) authorization.

### Proof of Concept
1. Account `A` (owner key `Ka`) creates an `Active` permission (id 2) with `threshold=1`, containing key `Kb` (weight 1), and an operations bitmap that includes bit 46 for `AccountPermissionUpdateContract` (e.g., a broad/default mask as produced by wallet tooling), via a legitimate `AccountPermissionUpdateContract` signed by `Ka` (see `AccountPermissionUpdateActuator.execute`, `AccountCapsule.updatePermissions`).
2. Attacker holding only `Kb` builds a new `AccountPermissionUpdateContract` for owner address `A` with:
   - `owner` = `Permission{type=Owner, threshold=1, keys=[Kb: weight 1]}` (dropping `Ka` entirely),
   - a valid `actives` list satisfying `checkPermission` structural rules.
3. Attacker sets `contract.getPermissionId() = 2` and signs the transaction with `Kb` only, then broadcasts it (e.g., via `AccountPermissionUpdateServlet`/`Wallet.broadcastTransaction`).
4. `TransactionCapsule.validateSignature` accepts the signature because `permission.getType()==Active` and bit 46 is set in its operations mask ( [2](#0-1) ).
5. `AccountPermissionUpdateActuator.validate()`/`execute()` accept and apply the new `Owner` permission unconditionally ( [4](#0-3) ), giving `Kb` sole `Owner` control of account `A` and permanently locking out `Ka`.

### Citations

**File:** chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java (L468-496)
```java
  public static boolean validateSignature(Transaction transaction,
      byte[] hash, AccountStore accountStore, DynamicPropertiesStore dynamicPropertiesStore)
      throws PermissionException, SignatureException, SignatureFormatException {
    Transaction.Contract contract = transaction.getRawData().getContractList().get(0);
    int permissionId = contract.getPermissionId();
    byte[] owner = getOwner(contract);
    AccountCapsule account = accountStore.get(owner);
    Permission permission = null;
    if (account == null) {
      if (permissionId == 0) {
        permission = AccountCapsule.getDefaultPermission(ByteString.copyFrom(owner));
      }
      if (permissionId == 2) {
        permission = AccountCapsule
            .createDefaultActivePermission(ByteString.copyFrom(owner), dynamicPropertiesStore);
      }
    } else {
      permission = account.getPermissionById(permissionId);
    }
    if (permission == null) {
      throw new PermissionException("permission isn't exit");
    }
    checkPermission(permissionId, permission, contract);
    long weight = checkWeight(permission, transaction.getSignatureList(), hash, null);
    if (weight >= permission.getThreshold()) {
      return true;
    }
    return false;
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

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L34-52)
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
