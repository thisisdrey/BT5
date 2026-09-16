### Title
Scoped Active-permission key with `AccountPermissionUpdateContract` bit set can overwrite Owner permission and wipe out all other Active permissions — cross-permission integrity violation - ([File: actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java])

### Summary
`AccountPermissionUpdateActuator` lets a transaction signed under *any* non-zero (Active) permission ID execute an `AccountPermissionUpdateContract`, provided that Active permission's `operations` bitmap has the `AccountPermissionUpdateContract` bit (type 46) set. Once such a scoped, lesser-privileged key is authorized for that single operation, it can submit a payload that unconditionally replaces the account's Owner permission and **all** Active permissions, silently destroying every other key/app scope on the account — exactly analogous to a shared-agent editor being able to delete/overwrite resources belonging to unrelated privileged owners.

### Finding Description
Signature/permission verification in `TransactionCapsule.checkPermission()` [1](#0-0)  only checks that the signer's own Active permission has the bit for `contract.getTypeValue()` set via `WalletUtil.checkPermissionOperations()` [2](#0-1) . It never restricts what an `AccountPermissionUpdateContract` payload may contain relative to the signer's own permission ID/scope.

In `AccountPermissionUpdateActuator.execute()`, the actuator unconditionally applies the entire new permission set from the contract payload — regardless of which permission ID authorized the transaction: [3](#0-2) 

`AccountCapsule.updatePermissions()` then overwrites the Owner permission and calls `clearActivePermission()` before repopulating **all** Active slots from the transaction, wiping out every previously configured Active permission (i.e., every other key/app/service scoped to that account): [4](#0-3) 

Validation of the new permission set (`checkPermission()`) only checks that operation bits set in a *new* Active permission are within `dynamicStore.getAvailableContractType()` — not `getActiveDefaultOperations()`. `AccountPermissionUpdateContract` (type 46) is present in `AVAILABLE_CONTRACT_TYPE` but deliberately excluded from the *default* `ACTIVE_DEFAULT_OPERATIONS` bitmap, as shown by the test's explicit exclusion of `ContractType.AccountPermissionUpdateContract` when computing the default active bitmap: [5](#0-4) 

This confirms the system's intent is that `AccountPermissionUpdateContract` should normally be excluded from Active-permission scopes — but nothing in `checkPermission()` (actuator) enforces this at validate-time: an owner (or a malicious dApp/service tricking the owner) can still explicitly include bit 46 in a scoped Active permission's `operations`, since `checkPermission()` only rejects bits outside `AVAILABLE_CONTRACT_TYPE`, not outside `ACTIVE_DEFAULT_OPERATIONS`: [6](#0-5) 

Once such a scoped key exists, its holder can sign an `AccountPermissionUpdateContract` transaction under their own (non-zero) permission ID and set a brand-new Owner permission (their own key, threshold satisfied by themselves) plus an entirely different Active permission list — deleting/overwriting every other Active permission slot used by unrelated keys/services on that account, and even seizing Owner control, with no check tying the payload's scope to the signer's original limited grant.

### Impact Explanation
This is a cross-scope integrity violation matching the reported bug class: a key intended to be scoped to a narrow, limited operation set can be leveraged to destroy/overwrite resources (other Active permissions, and ultimately Owner control) that belong to the account holder and other authorized parties who have no relationship to the scoped key. The result is unauthorized account takeover: the legitimate owner and any other apps relying on distinct Active permission slots on the same account lose access silently, and the attacker (holder of the mis-scoped key) can gain full Owner-level control of the account and all its funds/assets.

### Likelihood Explanation
This requires that some Active permission on the account has bit 46 (`AccountPermissionUpdateContract`) set in its `operations` field — which is not the default configuration but is explicitly permitted by validation, and is the kind of misconfiguration a wallet UI, third-party multisig tool, or malicious dApp could induce an owner into creating (e.g., "grant this key access to manage this feature" where the feature set is broader than expected, or a dApp requesting overly broad `operations` during permission setup). Given that once granted this key fully owns the account, likelihood should be assessed as realistic for any multisig account whose Active-permission grant flow does not carefully audit the `operations` bitmap before signing.

### Recommendation
- In `AccountPermissionUpdateActuator.checkPermission()`, reject the `AccountPermissionUpdateContract` bit (type 46) from being set in any Active permission's `operations`, mirroring the exclusion already enforced by default via `ACTIVE_DEFAULT_OPERATIONS`.
- Additionally, in `AccountPermissionUpdateActuator.validate()`/`execute()`, when the transaction is signed under a non-zero (Active) permission ID, restrict what the payload may modify (e.g., disallow altering permissions other than the signer's own Active slot, or require Owner-permission signature for any Owner/other-Active-permission modification).

### Proof of Concept
1. Account owner (or a compromised/careless flow) creates an Active permission (id=2) for a "helper" key with `operations` bitmap that includes bit 46 (`AccountPermissionUpdateContract`) alongside other intended bits (e.g., `TransferContract`). This passes `AccountPermissionUpdateActuator.checkPermission()` because bit 46 is present in `AVAILABLE_CONTRACT_TYPE`.
2. The helper key signs a new `AccountPermissionUpdateContract` transaction using `permission_id = 2`. `TransactionCapsule.checkPermission()`/`checkPermissionOperations()` passes because bit 46 is set on permission id=2.
3. The payload sets `owner` permission to a key controlled solely by the attacker (threshold satisfiable by that one key) and supplies a new `actives` list that no longer contains the legitimate other Active permissions (e.g., other dApp/service keys).
4. `AccountPermissionUpdateActuator.execute()` → `AccountCapsule.updatePermissions()` unconditionally applies this, replacing Owner permission and clearing/overwriting all Active permissions.
5. The true account owner and any other apps relying on their original Active permission slots are now locked out; the attacker holds full Owner control of the account.

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

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L43-52)
```java
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

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L132-145)
```java
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

**File:** framework/src/test/java/org/tron/core/actuator/AccountPermissionUpdateActuatorTest.java (L951-977)
```java
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
