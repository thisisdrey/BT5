### Title
Active permission holder can overwrite the Owner permission via `AccountPermissionUpdateContract`, permanently locking out or hijacking the account - (File: `actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java`)

### Summary
`AccountPermissionUpdateActuator` allows a transaction signed under any permission (including a lower-privileged `Active` permission, as long as that permission's operations bitmap allows `AccountPermissionUpdateContract`) to overwrite the account's `Owner` permission unconditionally. This mirrors the reported bug class: a role/permission tier that should be subordinate can destroy or reassign the "root" role, permanently locking legitimate owners out or handing account control to an attacker.

### Finding Description
`AccountPermissionUpdateActuator.execute()` unconditionally calls `account.updatePermissions(owner, witness, actives)`, which sets `builder.setOwnerPermission(owner)` no matter which permission tier authorized the transaction: [1](#0-0) [2](#0-1) 

Signature/permission verification for a transaction is done via `TransactionCapsule.validateSignature`/`checkPermission`, which only checks that the signing `Permission` (selected by `permission_id` in the contract) is of type `Active` and that its `operations` bitmap has the bit set for the contract type being executed - it does not restrict which contract types an `Active` permission is allowed to include: [3](#0-2) [4](#0-3) 

The actuator's own permission-content validation (`checkPermission`) checks the new `Active` permission's requested `operations` bitmap against `dynamicStore.getAvailableContractType()` — but this "available" list is *not* the restrictive default; it is confirmed by the test suite that `AccountPermissionUpdateContract` **is** included in `checkAvailableContractType` (the actual validation ceiling), while it is explicitly excluded only from `checkActiveDefaultOperations` (the convenience default used when creating a *new* account's default active permission): [5](#0-4) [6](#0-5) 

Consequently, nothing in `checkPermission()` (lines 71–146 of the actuator) prevents a user (via the default flow, or a governance/multisig setup that later delegates an active key with `AccountPermissionUpdateContract` enabled for convenience — e.g. automated key-rotation bots) from constructing an `Active` permission whose bitmap includes `AccountPermissionUpdateContract`. Once such an active key exists, any holder of that key can sign an `AccountPermissionUpdateContract` transaction (with `permission_id` pointing at that active permission) and overwrite the account's entire `Owner` permission — including setting the owner permission to keys the active-key holder controls, or to an unreachable threshold — with no requirement that the current Owner co-sign. [7](#0-6) 

### Impact Explanation
This breaks the intended privilege hierarchy of TRON's multisig permission model (Owner > Active), where Active permissions are meant to be scoped to specific, lower-risk contract types. If an Active key (which is often distributed more broadly for routine operations such as transfers, freezing, voting, etc.) is ever provisioned with the `AccountPermissionUpdateContract` bit set, that key alone can:
- Take over the account by rewriting the Owner permission to attacker-controlled keys (unauthorized account operation / theft of the account and all its assets), or
- Set an unreachable/inconsistent Owner permission (e.g., keys the legitimate owner does not control), permanently freezing the account's funds and future permission updates, since only the Owner permission can normally re-authorize `AccountPermissionUpdateContract` changes once compromised.

This matches the "permanent freezing of funds" / "unauthorized account operation" bar required by the validation rules.

### Likelihood Explanation
Exploitation requires that an `Active` permission with the `AccountPermissionUpdateContract` bit enabled exists and that the attacker controls (or compromises) one of its keys. This is not the out-of-the-box default (the default active permission excludes this contract type, per `checkActiveDefaultOperations`), so likelihood is contingent on account owners explicitly enabling this bit for an active permission (a supported, valid configuration the protocol does not prevent). Given TRON's common use of custodial/automated hot-wallet active keys for multisig-managed accounts, and that nothing in validation blocks this configuration or otherwise mitigates the resulting privilege escalation, this is a realistic misconfiguration/compromise scenario reachable purely through standard signed transactions (`AccountPermissionUpdateContract`), satisfying the "single signed transaction" reachability requirement.

### Recommendation
- Explicitly forbid `AccountPermissionUpdateContract` from ever being included in the `operations` bitmap of any `Active` (or `Witness`) permission in `AccountPermissionUpdateActuator.checkPermission()`, not just excluding it from the convenience default.
- Alternatively/additionally, require that `AccountPermissionUpdateContract` can only be executed via `permission_id == 0` (the Owner permission), enforced in `TransactionCapsule.checkPermission()`, so Active permissions can never modify Owner/Witness/Active permission structures regardless of their configured operations bitmap.

### Proof of Concept
1. Owner account `A` executes `AccountPermissionUpdateContract` to add an `Active` permission `P2` whose `operations` bitmap includes the bit for `AccountPermissionUpdateContract` (allowed today because `checkPermission()`'s bitmap check only validates against `getAvailableContractType()`, which includes this contract type, as shown by `checkAvailableContractType` test).
2. Attacker obtains/compromises the private key associated with `P2` (a lower-trust active key).
3. Attacker crafts a new `AccountPermissionUpdateContract` transaction for account `A`, sets `permission_id = P2.id`, and specifies a new `Owner` permission consisting solely of attacker-controlled keys.
4. `TransactionCapsule.checkPermission` only verifies `P2` is `Active` type and has the operations bit set (as in `checkPermission` at lines 635–645) — it does not block Owner permission modification via an Active key.
5. `AccountPermissionUpdateActuator.execute()` calls `account.updatePermissions(newOwner, ...)`, unconditionally overwriting `A`'s Owner permission (lines 44–52 of the actuator, lines 1301–1319 of `AccountCapsule`).
6. The attacker now fully controls account `A`'s Owner permission and can move funds/manage minters/etc.; the legitimate owner has permanently lost control unless they retained a still-valid Active key that itself has `AccountPermissionUpdateContract` rights (unlikely, and even then the attacker can race to change it first).

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

**File:** chainbase/src/main/java/org/tron/core/capsule/AccountCapsule.java (L1301-1319)
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

**File:** framework/src/test/java/org/tron/core/actuator/AccountPermissionUpdateActuatorTest.java (L919-977)
```java
  @Test
  public void checkAvailableContractTypeCorrespondingToCode() {
    // note: The aim of this test case is to show how the current codes work.
    // The default value is
    // 7fff1fc0037e0000000000000000000000000000000000000000000000000000,
    // and it should call the addSystemContractAndSetPermission to add new contract
    // type
    // When you add a new contact, you can add it to contractType,
    // as '|| contractType = ContractType.XXX',
    // and you will get the value from the output,
    // then update the value to checkAvailableContractType
    // and checkActiveDefaultOperations
    String validContractType = "7fff1fc0037ef80f000000000000000000000000000000000000000000000000";

    byte[] availableContractType = new byte[32];
    for (ContractType contractType : ContractType.values()) {
      if (contractType == org.tron.protos.Protocol.Transaction.Contract.ContractType.UNRECOGNIZED
          || contractType == ContractType.ClearABIContract
          || contractType == ContractType.UpdateBrokerageContract) {
        continue;
      }
      int id = contractType.getNumber();
      System.out.println("id is " + id);
      availableContractType[id / 8] |= (1 << id % 8);
    }

    System.out.println(ByteArray.toHexString(availableContractType));

    Assert.assertEquals(validContractType, ByteArray.toHexString(availableContractType));

  }

  @Test
  public void checkActiveDefaultOperationsCorrespondingToCode() {
    // note: The aim of this test case is to show how the current codes work.
    // The default value is
    // 7fff1fc0033e0000000000000000000000000000000000000000000000000000,
    // and it should call the addSystemContractAndSetPermission to add new contract
    // type
    String validContractType = "7fff1fc0033ef80f000000000000000000000000000000000000000000000000";

    byte[] availableContractType = new byte[32];
    for (ContractType contractType : ContractType.values()) {
      if (contractType == org.tron.protos.Protocol.Transaction.Contract.ContractType.UNRECOGNIZED
          || contractType == ContractType.AccountPermissionUpdateContract
          || contractType == ContractType.ClearABIContract
          || contractType == ContractType.UpdateBrokerageContract) {
        continue;
      }
      int id = contractType.getNumber();
      System.out.println("id is " + id);
      availableContractType[id / 8] |= (1 << id % 8);
    }

    System.out.println(ByteArray.toHexString(availableContractType));

    Assert.assertEquals(validContractType, ByteArray.toHexString(availableContractType));

  }
```

**File:** framework/src/test/java/org/tron/core/actuator/AccountPermissionUpdateActuatorTest.java (L979-1020)
```java
  @Test
  public void checkAvailableContractType() {
    String validContractType = "7fff1fc0037ef90f000000000000000000000000000000000000000000000000";

    byte[] availableContractType = new byte[32];
    for (ContractType contractType : ContractType.values()) {
      if (contractType == org.tron.protos.Protocol.Transaction.Contract.ContractType.UNRECOGNIZED
          || contractType == ContractType.UpdateBrokerageContract) {
        continue;
      }
      int id = contractType.getNumber();
      System.out.println("id is " + id);
      availableContractType[id / 8] |= (1 << id % 8);
    }

    System.out.println(ByteArray.toHexString(availableContractType));

    Assert.assertEquals(validContractType, ByteArray.toHexString(availableContractType));

  }

  @Test
  public void checkActiveDefaultOperations() {
    String validContractType = "7fff1fc0033ef90f000000000000000000000000000000000000000000000000";

    byte[] availableContractType = new byte[32];
    for (ContractType contractType : ContractType.values()) {
      if (contractType == org.tron.protos.Protocol.Transaction.Contract.ContractType.UNRECOGNIZED
          || contractType == ContractType.AccountPermissionUpdateContract
          || contractType == ContractType.UpdateBrokerageContract) {
        continue;
      }
      int id = contractType.getNumber();
      System.out.println("id is " + id);
      availableContractType[id / 8] |= (1 << id % 8);
    }

    System.out.println(ByteArray.toHexString(availableContractType));

    Assert.assertEquals(validContractType, ByteArray.toHexString(availableContractType));

  }
```
