### Title
Account owners can inadvertently grant an Active-permission key full account takeover via `AccountPermissionUpdateContract` because permission checks validate only a generic operation bit, not that Owner-level authority is required to rewrite the Owner permission - ([File: actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java])

### Summary
`AccountPermissionUpdateActuator.validate()`/`execute()` performs no check that the transaction was actually authorized by the account's Owner permission before it overwrites that account's Owner permission (and, for witnesses, the Witness permission). Authorization for non-default permissions is delegated entirely to a generic operations-bitmap check (`checkPermissionOperations`) performed during signature validation, which only confirms that the signing `Active` permission has the bit set for `ContractType.AccountPermissionUpdateContract` — it never distinguishes that this particular contract type rewrites the account's highest-privilege (`Owner`) permission. This mirrors the Mattermost analog: the system checks "does this actor have permission to perform this class of action" but not "is this actor privileged enough relative to the target being modified" — a lower-tier permission holder can therefore modify the account's top-tier (admin-equivalent) settings.

### Finding Description
`AccountPermissionUpdateActuator.execute()` unconditionally applies the new `Owner`/`Witness`/`Active` permissions supplied in the contract to the target account, using only the `ownerAddress` from the contract: [1](#0-0) 

Neither `validate()` nor `execute()` inspects which permission (Owner vs. Active) actually signed the enclosing transaction. That check lives outside the actuator, in `TransactionCapsule`, where a permission other than Owner (`permissionId != 0`) is authorized purely by checking whether the operations bitmap of that `Active` permission has the bit set for the contract's `ContractType`: [2](#0-1) [3](#0-2) 

The actuator's own `checkPermission()` (validation of the new permissions being set) explicitly allows `AccountPermissionUpdateContract`'s bit to be present in an `Active` permission's operations, since the node's `getAvailableContractType()` default set includes it: [4](#0-3) 

Notably, java-tron's own test suite shows the developers are aware this is a distinct, more dangerous capability: the **default, auto-generated** Active permission explicitly excludes `AccountPermissionUpdateContract` from its allowed operations: [5](#0-4) 

However, nothing in the actuator or in `checkPermissionOperations`/`checkPermission` prevents an account owner from later granting (or being tricked into granting, e.g., via a wallet/dApp requesting a seemingly narrow "Active" permission) a custom Active permission that includes this bit. Once granted, the holder of that Active key can sign an `AccountPermissionUpdateContract` transaction that completely replaces the account's Owner permission (removing the legitimate owner's keys) and, if the account is a witness, the Witness permission as well — using only Active-level authority to perform what is effectively an Owner-level, account-hijacking operation. This is the same class of bug as the Mattermost advisory: a lower-privileged principal (an Active-permission key, analogous to a "user manager") is able to modify the target's highest-privilege configuration (the Owner permission, analogous to "admin details") because the system checks a coarse capability bit instead of validating the actor's privilege level relative to what is being changed.

### Impact Explanation
Any key holding an `Active` permission whose operations bitmap includes the `AccountPermissionUpdateContract` bit can unilaterally rewrite the account's `Owner` permission, removing the original owner's control and installing attacker-controlled keys as the new owner. This is a concrete account-takeover / theft-of-funds vector: once the attacker controls the Owner permission, they control all subsequent transfers, resource delegation, and further permission changes for the account, effectively stealing it. This matches the "concrete unauthorized account operation, theft ... of funds" acceptance bar.

### Likelihood Explanation
Exploitation requires the account owner to have granted an Active permission whose 32-byte operations bitmap includes the bit for `AccountPermissionUpdateContract`, which the protocol allows by design (only the *default* generated Active permission excludes it) and which is not otherwise flagged as unusually dangerous to end users constructing custom multisig permissions. This is a realistic misconfiguration/social-engineering scenario (e.g., a dApp or exchange custody flow asking a user to grant a "just add this operation" Active permission that quietly includes account-permission-management rights).

### Recommendation
In `AccountPermissionUpdateActuator.validate()`/`execute()`, or in `TransactionCapsule.checkPermission()`, explicitly require that a transaction of type `AccountPermissionUpdateContract` be authorized only by the account's `Owner` permission (`permissionId == 0`), regardless of what the operations bitmap of any `Active` permission allows. Additionally, disallow setting the `AccountPermissionUpdateContract` bit in any `Active` permission's operations at actuator-validation time (mirroring the exclusion already applied to the default-generated Active permission).

### Proof of Concept
1. Account `A` (owner key `K_owner`) creates a custom `Active` permission with a low threshold, one key `K_active`, and an operations bitmap that includes the bit for `ContractType.AccountPermissionUpdateContract`, and calls `AccountPermissionUpdateContract` (signed by `K_owner`) to install it — this succeeds because the actuator's `checkPermission()` allows this bit for Active permissions.
2. Holder of `K_active` later crafts a new `AccountPermissionUpdateContract` transaction for account `A`, setting a new `Owner` permission containing only the attacker's key, sets `permissionId` to the Active permission's id, and signs with `K_active`.
3. At broadcast, `TransactionCapsule.validateSignature()` → `checkPermission()` only checks that the Active permission's operations bit for `AccountPermissionUpdateContract` is set (`checkPermissionOperations`), which it is; the transaction is accepted.
4. `AccountPermissionUpdateActuator.execute()` calls `account.updatePermissions(...)` and overwrites `A`'s Owner permission with the attacker's key, granting the attacker full control of account `A`. [6](#0-5)

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

**File:** framework/src/test/java/org/tron/core/actuator/AccountPermissionUpdateActuatorTest.java (L1000-1019)
```java
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
