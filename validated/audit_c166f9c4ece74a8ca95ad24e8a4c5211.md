### Title
Active-permission signer can rewrite the Owner permission via `AccountPermissionUpdateContract`, granting itself full account ownership - (File: `actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java`)

### Summary
The MantisBT advisory describes a backend handler (`ProjectUsersAddCommand`) that only performs structural/format validation on a caller-supplied "access level" value, never checking that the level being granted does not exceed the caller's own authority. The UI enforced the restriction, but the API/backend did not. The same class of bug exists in java-tron's multisig permission system: `AccountPermissionUpdateActuator.validate()` only checks the *structure* of the submitted `Permission` objects (key counts, weight sums, threshold, name length, operations bitmap format against the globally-enabled contract types). It never checks whether the *signer's own current permission/authority level* is sufficient to justify the *resulting* Owner permission being written. The only gate that could stop a low-privilege signer from calling this contract type at all is a generic, per-contract-type bitmap check performed elsewhere (`TransactionUtil.checkPermissionOperations`), which is unrelated to weight/threshold semantics and can be satisfied trivially.

### Finding Description
An account's authority model in java-tron is `Owner` (top-level, threshold+weighted keys) → `Active` (delegated, restricted to a per-contract-type bitmap of allowed operations) → optional `Witness`. A user can delegate an `Active` permission to another key and restrict which `ContractType`s that key may invoke via the 256-bit `operations` bitmap, as validated in `checkPermission`: [1](#0-0) 

Critically, `AccountPermissionUpdateContract` itself is one of the values in the `ContractType` enum and can be included in that bitmap like any other contract type — there is nothing in `checkPermission`/`validate()` that special-cases or forbids granting the `AccountPermissionUpdateContract` bit to an `Active` permission: [2](#0-1) 

When a transaction is signed with a non-zero `permissionId` (i.e., using an `Active` key rather than the `Owner` key), the only authorization check performed before dispatching to the actuator is whether the signer's `Active` permission bitmap has the bit set for the transaction's `ContractType`, plus a weight/threshold check against that same (lower) `Active` permission — it does **not** compare the *content* of the contract being executed against the signer's authority level: [3](#0-2) [4](#0-3) 

So if an account owner grants an `Active` key the `AccountPermissionUpdateContract` bit (e.g., using a permissive/broad custom operations bitmap, which is common for "manager"-style delegated keys), that `Active` key — despite having strictly *lower* privilege than `Owner` — can sign and successfully execute an `AccountPermissionUpdateContract` transaction. `AccountPermissionUpdateActuator.execute()` then unconditionally overwrites the account's `Owner` permission (and Active/Witness permissions) with whatever the transaction specifies: [5](#0-4) [6](#0-5) 

Nothing in `validate()`/`checkPermission()` verifies that the new `Owner` permission's keys/threshold are consistent with, or bounded by, the authority of the signer who authorized the change — it only validates the standalone structural correctness of the submitted `Permission` message (weight sum ≥ threshold, valid addresses, no duplicate keys, etc.), exactly analogous to how MantisBT's `ProjectUsersAddCommand` validated the shape of the request but not whether the requested `access_level` exceeded the actor's own level.

### Impact Explanation
An account owner who delegates an `Active` permission that includes the `AccountPermissionUpdateContract` operation to a "manager"-tier signer effectively grants that signer the ability to become full `Owner` of the account: the signer can submit an `AccountPermissionUpdateContract` transaction that sets the `Owner` permission's key list to itself alone with threshold 1, completely displacing the original owner's control. This is unauthorized privilege escalation and results in unauthorized account takeover / control of funds and permissions, matching the "concrete unauthorized account operation" bar for validity.

### Likelihood Explanation
Exploitation requires that the account owner has previously delegated an `Active` permission whose operations bitmap includes the `AccountPermissionUpdateContract` bit to a third party (a realistic and common multisig/delegation configuration, since operators often grant broad "manager" style Active keys rather than hand-crafting minimal bitmaps per contract type). Given such a delegation exists, any holder of that Active key can unilaterally escalate to full ownership with a single signed transaction — no additional cooperation or race condition needed.

### Recommendation
`AccountPermissionUpdateActuator.validate()` should reject any attempt to modify a `Permission` when the transaction's own `permissionId` used to authorize it is not the `Owner` permission id (0) — i.e., forbid using an `Active`/`Witness` permission to invoke `AccountPermissionUpdateContract` at all, or otherwise ensure only the current `Owner` permission of the account can update the `Owner` permission. Alternatively, treat `AccountPermissionUpdateContract` as a special/reserved `ContractType` that can never be enabled in an `Active` permission's operations bitmap, enforced in `checkPermission` similarly to how other invariants (parent id, key count) are enforced there.

### Proof of Concept
1. Owner account `A` calls `AccountPermissionUpdateContract` to create an `Active` permission for delegate key `B`, with an `operations` bitmap that includes the bit for `AccountPermissionUpdateContract` (e.g. a broadly permissive "manager" bitmap or the default active bitmap if it enables most/all types), threshold 1, weight 1.
2. `B` signs a new `AccountPermissionUpdateContract` transaction using `permission_id` referencing its own `Active` permission, setting the `Owner` permission to a single key (`B`'s address) with threshold 1, and empty/self-serving `Active`/`Witness` permissions.
3. `TransactionUtil.checkPermissionOperations` passes because the `AccountPermissionUpdateContract` bit is set in `B`'s `Active` permission bitmap; `TransactionCapsule.checkWeight` passes because `B`'s own signature meets `B`'s `Active` threshold.
4. `AccountPermissionUpdateActuator.validate()` passes because the submitted `Owner`/`Active` permissions are structurally valid.
5. `AccountPermissionUpdateActuator.execute()` overwrites account `A`'s `Owner` permission with `B` as sole owner, completing the privilege escalation.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L41-59)
```java
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
