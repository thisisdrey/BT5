### Title
Active permission holders can execute `AccountPermissionUpdateContract` to overwrite the Owner permission, escalating to full account control - ([File: actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java])

### Summary
`AccountPermissionUpdateActuator` applies an owner/witness/active permission rewrite to an account without checking that the specific permission (owner vs. an "active" delegated key) which signed the transaction actually had the authority to redefine ownership itself. Because `AccountPermissionUpdateContract` is a normal `ContractType` value not excluded from the "available contract types" bitmap that an Active permission's `operations` field can legally include, an account owner (or an attacker who otherwise obtains signing rights for an Active key, e.g. through a compromised delegated/limited key) can grant that Active permission the right to invoke `AccountPermissionUpdateContract`. Once that is done, any signer meeting only the (typically much lower) Active-permission threshold can call `AccountPermissionUpdateContract` and completely rewrite the account's Owner permission, Witness permission, and all Active permissions — i.e., escalate to full ownership and lock out or remove the legitimate owner keys. This mirrors the reported pattern where a supposedly-limited role (`MINTER_ROLE`) can be leveraged, via a loosely-guarded privilege-granting primitive, to bypass the intended access-control tiering.

### Finding Description
`AccountPermissionUpdateActuator.execute()` unconditionally calls `AccountCapsule.updatePermissions()` with whatever `owner`, `witness`, and `actives` permissions are present in the incoming `AccountPermissionUpdateContract`: [1](#0-0) 

There is no logic in `execute()` (or `validate()`) that inspects `permissionId` of the transaction to confirm the signer used the Owner permission (`permissionId == 0`) rather than a delegated Active permission (`permissionId >= 2`) before rewriting the account's entire permission hierarchy: [2](#0-1) 

The only gate that restricts which contract types an Active-permission key can invoke is the `operations` bitmask check performed generically for every transaction in `TransactionCapsule.checkPermission`/`checkPermissionOperations`, which is contract-type agnostic — it does not special-case or forbid `AccountPermissionUpdateContract`: [3](#0-2) [4](#0-3) 

`AccountPermissionUpdateActuator.checkPermission()` (the internal validator, not to be confused with `TransactionCapsule.checkPermission`) validates that the operations bitmask supplied for a new Active permission is a subset of `dynamicStore.getAvailableContractType()`, but it does not exclude `AccountPermissionUpdateContract` from that available set: [5](#0-4) 

The test `checkAvailableContractTypeCorrespondingToCode` documents that the default "available contract type" bitmap is built from all `ContractType` values except `UNRECOGNIZED`, `ClearABIContract`, and `UpdateBrokerageContract` — `AccountPermissionUpdateContract` is not in that exclusion list, confirming it is a permissible operation bit for an Active permission: [6](#0-5) 

Consequently, once an account owner (deliberately or by mistake) grants an Active permission the `AccountPermissionUpdateContract` operation bit — a completely legal and unblocked configuration — any signer satisfying that Active permission's (potentially very low) threshold can subsequently submit a new `AccountPermissionUpdateContract` transaction that redefines the Owner permission, Witness permission, and all Active permissions of the account, seizing full control and permanently locking out the legitimate owner keys.

### Impact Explanation
This allows unauthorized account takeover: a signer holding only a delegated, lower-privilege Active key (analogous to a "minter"/limited role in the original report) can use that limited privilege to rewrite the Owner permission and thereby gain full, unrestricted control of the account — including all subsequent fund movement, resource delegation, and future permission changes — while the true owner keys can be removed from the new Owner permission set, resulting in permanent loss of control (and potentially freezing) of the account's assets by the legitimate owner. This satisfies "unauthorized account operation" / "permanent freezing of funds" criteria.

### Likelihood Explanation
Requires that an account owner has configured an Active permission whose `operations` bitmask includes the `AccountPermissionUpdateContract` bit and whose threshold is satisfiable by fewer/weaker keys than the true Owner permission (a realistic multisig/delegation misconfiguration, and there is no protocol-level warning or restriction preventing it). Given TRON's multisig feature is commonly used to delegate limited operational authority to hot/automation keys, and nothing in `AccountPermissionUpdateActuator` or the available-contract-type bitmap prevents this specific escalation path, likelihood is moderate — it depends on account owners' permission configuration choices, but the protocol provides no safeguard against this dangerous configuration nor validates the signer's permission tier is appropriate for a full permission rewrite.

### Recommendation
In `AccountPermissionUpdateActuator.validate()`/`execute()`, require that transactions of type `AccountPermissionUpdateContract` can only be authorized by the account's Owner permission (`permissionId == 0`), rejecting any attempt to invoke this contract type via an Active permission. Alternatively/additionally, exclude `AccountPermissionUpdateContract` (and other self-privilege-modifying contract types) from the default/allowed `operations` bitmask that can be assigned to Active permissions, so it can never be granted to a delegated key.

### Proof of Concept
1. Owner account `A` has default Owner permission (threshold N, keys = owner's real keys).
2. Owner configures an Active permission `P2` with `operations` bitmask that includes the `AccountPermissionUpdateContract` bit (this is legal per `checkPermission()`/`getAvailableContractType()`), threshold = 1, keys = [delegatedKey].
3. Holder of `delegatedKey` (or an attacker who compromises just this key) builds an `AccountPermissionUpdateContract` transaction for account `A`, signs it with `permissionId = 2` (the Active permission id), setting a brand-new Owner permission whose only key is the attacker's key.
4. `TransactionCapsule.checkPermission` only checks that `AccountPermissionUpdateContract`'s type bit is set in `P2.operations` — it passes.
5. `AccountPermissionUpdateActuator.execute()` calls `account.updatePermissions(newOwner, witness, actives)` unconditionally, per [7](#0-6) , replacing the legitimate Owner permission with the attacker's.
6. Account `A` is now fully controlled by the attacker; original owner keys no longer have any permission entry.

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

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L124-146)
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
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L148-215)
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

**File:** framework/src/test/java/org/tron/core/actuator/AccountPermissionUpdateActuatorTest.java (L919-947)
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
```
