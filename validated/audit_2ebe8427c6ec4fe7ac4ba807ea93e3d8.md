## Title
Active (non-owner) permission holder can self-escalate to full Owner/Witness control via `AccountPermissionUpdateActuator` - (File: `actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java`)

### Summary
The DolphinScheduler bug class is a missing authorization/scope check that lets a lower-privileged principal mint credentials with admin-level rights. The closest reachable analog in java-tron is in `AccountPermissionUpdateActuator`, where the `operations` bitmap of a user-defined **Active** permission is validated only against the global `DynamicPropertiesStore.getAvailableContractType()` allow-list, not against a scope restriction that would prevent a scoped/delegated Active key from acquiring the right to call `AccountPermissionUpdateContract` itself. Because owning that single contract-type bit is sufficient to rewrite the entire account's Owner, Witness, and all Active permissions in one execution, a signer whose weight only satisfies a low-threshold Active permission can unilaterally seize full Owner-level control of the account, bypassing the (typically much higher) Owner threshold entirely.

### Finding Description
`AccountPermissionUpdateActuator.checkPermission()` validates the `operations` bitmap of an `Active` permission solely against the system-wide allow-list `dynamicStore.getAvailableContractType()`: [1](#0-0) 

`getAvailableContractType()` includes essentially every `ContractType`, including `AccountPermissionUpdateContract` itself, as confirmed by the test asserting the full mask `7fff1fc0037e...` covers all types except `UNRECOGNIZED`/`ClearABIContract`/`UpdateBrokerageContract`: [2](#0-1) 

By contrast, the codebase explicitly excludes `AccountPermissionUpdateContract` from the *default* Active operations mask, `checkActiveDefaultOperationsCorrespondingToCode`, showing the design intent that ordinary Active/delegated permissions should not normally hold this capability: [3](#0-2) 

However, nothing in `checkPermission()` or `validate()` enforces that exclusion for a *newly submitted* Active permission — a transaction can freely set the `AccountPermissionUpdateContract` bit in an Active permission's `operations` field, and it passes validation because that bit is present in `getAvailableContractType()`.

Once a permission is created that grants an Active key this operation, the downstream signature-authorization path treats it as fully sufficient to invoke `AccountPermissionUpdateContract` — with **no additional comparison to the Owner permission's threshold**: [4](#0-3) [5](#0-4) 

The actuator's `execute()` then blindly overwrites Owner, Witness, and all Active permissions using `AccountCapsule.updatePermissions()` with whatever values were supplied in the contract — including the Owner permission — regardless of which permission (Owner vs. lower-threshold Active) authorized the call: [6](#0-5) [7](#0-6) 

The net effect: a signer who only ever needed to meet a modest Active-permission threshold (e.g. `threshold=1` with a single low-weight key, intended for narrow, delegated operations) can, if that Active permission's operations bitmap includes the `AccountPermissionUpdateContract` bit, submit a single `AccountPermissionUpdateContract` transaction that installs a brand-new Owner permission (e.g. `threshold=1`, key = attacker-controlled address) and rewrite all Active/Witness permissions — completely displacing the legitimate high-threshold Owner co-signers. This mirrors the DolphinScheduler pattern of a lower-privileged actor being able to mint themselves admin-equivalent credentials because the endpoint fails to bound the scope of what a delegated/limited credential can grant.

### Impact Explanation
This allows permanent, unauthorized account takeover: an attacker holding (or compromising) only one key from a multi-sig Active permission that was configured/misconfigured with `AccountPermissionUpdateContract` access can eliminate the original Owner permission's threshold protections in a single transaction, seizing full control (transfers, resource delegation, voting, further permission changes) of the account. This is a concrete "unauthorized account operation / permanent loss of control" outcome.

### Likelihood Explanation
Exploitation requires that some Active permission on the target account has the `AccountPermissionUpdateContract` operation bit enabled — which is not the default, but is not blocked by validation either, and TRON's own permission model advertises `AccountPermissionUpdateContract` as one of the addressable operation types for Active permissions (it appears in `getAvailableContractType()`). Any account owner (or wallet/dApp tooling) that grants a co-signer or automated key an Active permission intended to include "manage permissions" functionality — without realizing it also permits full Owner/Witness permission replacement independent of the Owner threshold — creates this exposure. Given TRON's widespread use of multi-sig Active permissions for exchanges/custodians, misconfiguration of this scope is plausible and, once present, is trivially and immediately exploitable by any holder of the affected key.

### Recommendation
In `AccountPermissionUpdateActuator.checkPermission()`, explicitly forbid the `AccountPermissionUpdateContract` operation bit within any `Active`-type permission's `operations` field (mirroring the exclusion already enforced for the *default* active operations mask), so that only the Owner permission itself can ever authorize `AccountPermissionUpdateContract` transactions. Alternatively/additionally, require that any `AccountPermissionUpdateContract` execution be authorized against the account's current Owner permission threshold regardless of which `permissionId` signed the transaction, rather than allowing an Active permission's own (lower) threshold to suffice.

### Proof of Concept
1. Account `A` has Owner permission `threshold=3` with 3 independent keys (`k1,k2,k3`), and additionally grants an Active permission `P2` (`threshold=1`, single key `kAttacker`) whose `operations` bitmap includes the bit for `AccountPermissionUpdateContract` (this passes `checkPermission()` validation since that bit is present in `getAvailableContractType()`, per `checkAvailableContractTypeCorrespondingToCode`).
2. Attacker, holding only `kAttacker`, builds an `AccountPermissionUpdateContract` transaction with `permission_id = 2` (referencing `P2`), setting:
   - New Owner permission: `threshold=1`, single key = attacker's own address.
   - New Active permission(s): attacker-controlled.
3. Attacker signs with `kAttacker` only. `TransactionCapsule.checkPermission()`/`checkPermissionOperations()` succeeds because `P2`'s operations bitmap includes `AccountPermissionUpdateContract`, and `checkWeight()` succeeds because `kAttacker`'s weight already meets `P2.threshold=1`.
4. `AccountPermissionUpdateActuator.execute()` calls `account.updatePermissions(newOwner, ...)`, replacing the original 3-of-3 Owner permission with the attacker's 1-of-1 Owner permission.
5. Attacker now fully controls account `A` with no further need for `k1, k2, k3`, despite never having met the original Owner threshold.

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
