### Title
Active Permission Scoped to AccountPermissionUpdateContract Can Overwrite Owner/Witness Permissions - (File: `actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java`)

### Summary
The reported edx-platform bug is a scope-confusion privilege escalation: a role granted at a coarse scope (org) is honored as if it were granted at a finer, more-privileged scope (course/studio) that it was never meant to reach. The closest reachable analog in java-tron is in the account multisig permission model: any narrowly-scoped `Active` permission that merely has the `AccountPermissionUpdateContract` bit set in its `operations` bitmap is treated as sufficient authority to completely rewrite the account's `Owner` permission, `Witness` permission, and every `Active` permission slot — even though that key was only meant to be authorized for a specific, limited set of contract types.

### Finding Description
`AccountPermissionUpdateActuator.execute()` unconditionally calls `account.updatePermissions(...)` with the new Owner/Witness/Actives supplied in the transaction, with no check on which permission tier was used to authorize the transaction beyond the generic signature check performed earlier in the pipeline: [1](#0-0) 

That generic signature check, in `TransactionCapsule.validateSignature`/`checkPermission`, only verifies that if a non-owner `permissionId` is used, the permission's `type` is `Active` and its `operations` bitmap has the bit set for the contract type being executed (`AccountPermissionUpdateContract` in this case): [2](#0-1) [3](#0-2) 

`AccountPermissionUpdateActuator.checkPermission()` (the structural `validate()` helper) only validates the *shape* of the new permissions being set (key count, threshold, distinct addresses, valid operations bitmap) — it never checks that the permission used to sign the transaction is the `Owner` permission, or that the new permission set is a subset/no-broader than what the signing key was scoped to: [4](#0-3) 

`AccountCapsule.updatePermissions()` then blindly replaces `ownerPermission`, `witnessPermission`, and the entire `activePermission` list: [5](#0-4) 

The result is that a key holding only a narrow-scope `Active` permission (id ≥ 2, with a restricted `operations` bitmap that happens to include the `AccountPermissionUpdateContract` bit — a bit that is often granted alongside other operational bits for key-rotation convenience) is architecturally equivalent to holding the `Owner` (id 0) permission: it can rewrite the `Owner` permission's keys/threshold, delete or replace the `Witness` permission, and redefine every other `Active` permission slot. This mirrors the CVE's core defect class: authority intended to be scoped to a narrow purpose is silently honored at the broadest, most-privileged scope.

### Impact Explanation
Any account owner who grants an `Active` permission with the `AccountPermissionUpdateContract` operation bit set — intending only to allow, e.g., convenience key rotation for that specific permission slot — inadvertently grants that key full unilateral control of the account: it can seize `Owner` permission (drain funds via any subsequently signed Owner-authorized transaction), remove all other signers, or hijack the `Witness` permission of a witness account. This is a concrete path to unauthorized account takeover / permanent loss of control over funds and voting/witness authority, reachable by a single signed transaction from a party who was only meant to hold narrow, delegated signing authority.

### Likelihood Explanation
Exploitation requires only that the attacker control (or was delegated) an `Active` permission key whose `operations` bitmap includes the `AccountPermissionUpdateContract` bit — a common real-world configuration for multisig setups that want to allow permission/key rotation without giving out the Owner key. No additional privilege, insider access, or malicious-SR/witness collusion is needed; it is triggerable directly by broadcasting a normal signed `AccountPermissionUpdateContract` transaction.

### Recommendation
In `AccountPermissionUpdateActuator.validate()`, require that transactions modifying `Owner` (and ideally `Witness`) permissions be authorized specifically via the `Owner` permission (`permissionId == 0`), rejecting attempts to change `ownerPermission`/`witnessPermission` when the signing permission is a scoped `Active` permission. Alternatively, restrict what an `Active`-signed `AccountPermissionUpdateContract` may alter to permissions with an equal-or-lower privilege tier than the signing permission itself.

### Proof of Concept
1. Account `A` sets up multisig: `Owner` permission held by key `K_owner`; an `Active` permission (id=2) held by key `K_limited` with an `operations` bitmap that only includes bits for, e.g., `TransferContract` and `AccountPermissionUpdateContract` (a plausible "allow this key to rotate itself" configuration).
2. Attacker who controls `K_limited` crafts an `AccountPermissionUpdateContract` transaction with `permission_id = 2`, setting a brand-new `Owner` permission whose only key is attacker-controlled, and setting new `Active`/`Witness` permissions as desired.
3. The transaction passes `TransactionCapsule.validateSignature` (permission type is `Active`, bit for `AccountPermissionUpdateContract` is set) and `AccountPermissionUpdateActuator.validate()` (structural checks only).
4. `AccountPermissionUpdateActuator.execute()` calls `account.updatePermissions(...)`, replacing the real `Owner` permission with the attacker's, giving the attacker full unilateral control of account `A`.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L44-52)
```java
    try {
      accountPermissionUpdateContract = any.unpack(AccountPermissionUpdateContract.class);

      byte[] ownerAddress = accountPermissionUpdateContract.getOwnerAddress().toByteArray();
      AccountCapsule account = accountStore.get(ownerAddress);
      account.updatePermissions(accountPermissionUpdateContract.getOwner(),
          accountPermissionUpdateContract.getWitness(),
          accountPermissionUpdateContract.getActivesList());
      accountStore.put(ownerAddress, account);
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

**File:** chainbase/src/main/java/org/tron/core/capsule/AccountCapsule.java (L1301-1320)
```java
  public void updatePermissions(Permission owner, Permission witness, List<Permission> actives) {
    Builder builder = this.account.toBuilder();

    owner = owner.toBuilder().setId(0).build();
    builder.setOwnerPermission(owner);
    if (witness != null && builder.getIsWitness()) {
      witness = witness.toBuilder().setId(1).build();
      builder.setWitnessPermission(witness);
    }

    builder.clearActivePermission();
    if (actives != null) {
      for (int i = 0; i < actives.size(); i++) {
        Permission permission = actives.get(i).toBuilder().setId(i + 2).build();
        builder.addActivePermission(permission);
      }
    }

    this.account = builder.build();
  }
```
