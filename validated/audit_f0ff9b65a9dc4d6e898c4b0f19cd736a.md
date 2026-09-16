### Title
Privilege escalation via `AccountPermissionUpdateContract`: an Active-permission key with the `AccountPermissionUpdateContract` operation bit set can unilaterally overwrite the Owner permission and seize full account control - (File: `actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java`)

### Summary
The Mattermost CVE (BIT-mattermost-2023-2515) describes a user holding a narrow "edit other users" permission who is able to leverage that permission to grant itself full system-admin rights, because the system fails to restrict what a lower-privileged edit capability can ultimately change. The same bug class exists in java-tron's multisig permission model: TRON's Active permissions can be granted the right to invoke `AccountPermissionUpdateContract`, and once granted, the actuator that executes it applies zero cross-check between "who is authorizing this call" and "what the call is allowed to rewrite" - it lets that same Active key overwrite the account's Owner permission, Witness permission, and all Active permissions in a single unsupervised transaction.

### Finding Description
`AccountPermissionUpdateActuator.validate()` only checks the *shape* of the new permission set (threshold, key count, distinct addresses, operations bitmap validity) - it never checks that the entity currently authorizing this transaction is restricted from altering the Owner permission: [1](#0-0) 

`execute()` then blindly overwrites the account's Owner/Witness/Active permission set from the contract fields with no comparison to the previous state or the calling key's actual authority level over the Owner role: [2](#0-1) 

Authorization for which key may submit this contract is determined solely by `contract.getPermissionId()` pointing at whichever permission (Owner=0 or a custom Active id) the signer used, verified generically in `TransactionCapsule.checkPermission`/`checkWeight`, which only confirms the operations bitmap allows the contract type - it applies the exact same rule to `AccountPermissionUpdateContract` as to any other contract type: [3](#0-2) [4](#0-3) [5](#0-4) 

Critically, the actuator's own `checkPermission()` bit-validation for an Active permission's `operations` field only rejects bits that are not part of `getAvailableContractType()` - and `AccountPermissionUpdateContract` **is** part of that available set (unlike `ClearABIContract`/`UpdateBrokerageContract`, which are excluded), so nothing in the validate path stops an Active permission from being configured (or later modified) to include the AccountPermissionUpdateContract bit: [6](#0-5) 

Tron's own test suite acknowledges this is a deliberately dangerous capability - `AccountPermissionUpdateContract` is explicitly excluded only from the *default* Active-permission operations bitmap (`ACTIVE_DEFAULT_OPERATIONS`), confirming the developers recognize that allowing an Active key to call this contract is unsafe by default, yet no runtime enforcement in the actuator prevents it from being explicitly enabled: [7](#0-6) 

Once such an Active permission exists (e.g., created by the real owner for a delegate, an exchange operations key, or a compromised custody-tool default), that key holder alone can submit `AccountPermissionUpdateContract` with a completely new `owner` `Permission` message naming only their own address/key, permanently removing the original owner's authority, exactly mirroring the Mattermost pattern of a narrow edit-permission being abused to reach full admin.

### Impact Explanation
An account whose owner has granted a subordinate/delegate key an Active permission including the `AccountPermissionUpdateContract` bit (a supported, non-default but fully valid configuration) gives that delegate unilateral capability to seize full Owner control of the account: overwrite the Owner permission's keys/threshold, delete or alter Witness permission, and rewrite all Active permissions - effectively locking out the legitimate owner and any co-signers. This is a concrete unauthorized-account-takeover / permanent loss-of-control impact meeting the High-severity bar (funds and account governance can be permanently redirected to the attacker's own keys with no possibility for the original owner to reverse it on-chain).

### Likelihood Explanation
Exploitation requires only that a delegate/employee/relayer key be granted an Active permission whose `operations` bitmap includes `AccountPermissionUpdateContract`. This is not blocked by validation, is technically a "supported" custom configuration (the field is documented and used in `UpdateAccountServletTest`), and is plausible in real deployments (multisig custody setups, exchange hot-wallet delegate keys, DAO treasury managers) where an operator assumes that granting "administrative" active permissions is safe short of full Owner rights. Because there is no additional confirmation, timelock, or Owner-only enforcement on this specific contract type, likelihood is Medium-High for any account using non-default custom Active permissions.

### Recommendation
Add an explicit, non-bypassable restriction in `AccountPermissionUpdateActuator.validate()`/`execute()` (and in the bit-validation of `checkPermission()`) that permanently forbids the `AccountPermissionUpdateContract` type from ever being included in an Active permission's `operations` bitmap (not just excluded by default), so that only the Owner permission (permissionId 0) can ever authorize permission-structure changes - closing the analog "narrow permission escalates to full admin" path described in the Mattermost advisory.

### Proof of Concept
1. Owner account `A` creates a custom Active permission (id=2) for delegate key `D` with `operations` bitmap manually set to include bit 46 (`AccountPermissionUpdateContract`), alongside normal operations - a configuration the actuator's `checkPermission()` fully accepts since that bit is in `getAvailableContractType()`.
2. Delegate `D` (holding only this Active permission, not Owner) crafts an `AccountPermissionUpdateContract` transaction for account `A`, setting `permission_id = 2` in the contract, and supplies a new `owner` `Permission` naming only `D`'s address with threshold 1.
3. `TransactionCapsule.checkPermission`/`checkWeight` validates `D`'s signature against the Active permission (id 2) and its operations bitmap - passes because bit 46 is set.
4. `AccountPermissionUpdateActuator.validate()`/`execute()` accepts and applies the new Owner permission verbatim via `account.updatePermissions(...)`, replacing the original Owner permission entirely.
5. `D` now solely controls account `A`'s Owner permission; the original owner's keys are permanently removed from Owner authority, with no on-chain path to reverse it - full account takeover achieved from a permission originally scoped only to non-owner "active" operations.

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

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L208-221)
```java
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
