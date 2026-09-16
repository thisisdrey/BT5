## Title
Active-permission key can escalate to full account-owner control via `AccountPermissionUpdateContract` — ([File: actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java])

### Summary
An account's low-privilege "Active" permission key, if it has ever been granted the `AccountPermissionUpdateContract` operation bit, can independently rewrite the account's `Owner` permission (and any other Active/Witness permissions) to values of its own choosing — with only the Active permission's threshold required, not the Owner permission's threshold. This mirrors CVE-2019-11816's "incorrect access control ... allows ... privilege escalation to administrator via a specially crafted request": a lesser-privileged, authenticated signer reaches an administrative operation it should not be able to fully control.

### Finding Description
`AccountPermissionUpdateActuator.checkPermission()` validates a submitted permission's `operations` bitmap only against the chain-wide list of *existing* contract types (`dynamicStore.getAvailableContractType()`), not against any restricted subset that excludes sensitive permission-management operations: [1](#0-0) 

The framework test suite confirms `AccountPermissionUpdateContract` is present in the full "available contract type" bitmap accepted by this check, even though it is deliberately *excluded* from the "active default operations" bitmap used when accounts are created: [2](#0-1) 

Separately, transaction-level permission enforcement (`WalletUtil.checkPermissionOperations` / `TransactionCapsule.checkPermission`) only checks whether the signer's Active permission has the corresponding contract-type bit set — it does not distinguish between "update my own active keys" and "replace the account's owner permission": [3](#0-2) [4](#0-3) 

Once that gate passes, `AccountCapsule.updatePermissions()` unconditionally overwrites the account's Owner permission with whatever `Permission` object was submitted in the contract, regardless of which permission (Owner or Active) authorized the transaction: [5](#0-4) 

So the security boundary that should exist — "only the Owner permission (threshold-weighted keys) may redefine the Owner permission" — is not enforced anywhere in the actuator or capsule. Any Active permission that carries the `AccountPermissionUpdateContract` bit is functionally equivalent to Owner for the purpose of permission management, but can be configured with a much lower threshold/weight than Owner.

### Impact Explanation
If an account owner (or a multisig configuration/tool) ever grants the `AccountPermissionUpdateContract` bit to an Active permission — e.g., to allow a low-threshold operational key to rotate its own Active keys without invoking the high-threshold Owner key — that same key can instead submit an `AccountPermissionUpdateContract` transaction that replaces the Owner permission with attacker-controlled keys/threshold. This permanently locks the legitimate owner out of the account and hands full unilateral control (including future transfers, freezing/unfreezing, voting, and further permission changes) to the holder of the previously lesser-privileged Active key. This satisfies "concrete unauthorized account operation" and "permanent freezing of funds" impact criteria.

### Likelihood Explanation
Exploitation requires only that an Active permission on the target account has the `AccountPermissionUpdateContract` operation bit enabled — a configuration state reachable through ordinary, unprivileged use of the multisig feature (an account owner delegating permission management to a lower-threshold Active key, a common pattern for exchanges/custodians managing hot-wallet key rotation). No special network position, code injection, or witness/committee collusion is required — a single signed transaction from the holder of that Active key is sufficient.

### Recommendation
Enforce that only the Owner permission (or a permission explicitly authorized at Owner-equivalent threshold) may submit an `AccountPermissionUpdateContract` that redefines the `Owner` permission. At minimum, `AccountPermissionUpdateActuator.validate()`/`execute()` should reject Owner-permission changes unless `contract.getPermissionId() == 0` (i.e., the Owner key itself authorized the change), and Active permissions should be structurally prevented from ever including the `AccountPermissionUpdateContract` operation bit for anything beyond updating their own Active/Witness scope.

### Proof of Concept
1. Account `A` is configured with an Owner permission (threshold 5, keys K1..K5) and an Active permission `P2` with threshold 1, containing key `Kx`, whose `operations` bitmap has the bit for `AccountPermissionUpdateContract` set (this is a valid configuration accepted by `checkPermission`, since that contract type is part of `getAvailableContractType()`).
2. Holder of `Kx` (who was only meant to authorize routine, low-value operations) crafts an `AccountPermissionUpdateContract` transaction with `permission_id = 2` (pointing at `P2`), setting a brand-new `Owner` permission containing only `Kx` with threshold 1.
3. The transaction passes `TransactionCapsule.checkPermission` (Active type + bit set) and `checkWeight` (threshold 1 satisfied by `Kx` alone), then `AccountPermissionUpdateActuator.execute()` calls `account.updatePermissions(...)`, unconditionally overwriting the Owner permission.
4. Account `A` is now fully controlled by `Kx` alone; the original 5-of-5 Owner keys K1-K5 are permanently locked out.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L132-144)
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
