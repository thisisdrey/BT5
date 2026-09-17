### Title
Privilege escalation from a limited Active permission to full Owner control via unrestricted `AccountPermissionUpdateContract` operation bit - (File: `actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java`)

### Summary
`AccountPermissionUpdateActuator` lets any account with an **Active** (non-owner) multisig permission whose `operations` bitmap happens to include the `AccountPermissionUpdateContract` bit rewrite the account's **Owner** permission itself. This mirrors GHSA-9x4q-3gxw-849f: a scoped, "limited" permission (`admin:users` in JupyterHub / an Active permission in TRON) is not actually restricted from granting itself the top-level administrative capability (`admin=True` in JupyterHub / Owner permission in TRON).

### Finding Description
TRON's account multisig model has three permission tiers: `Owner` (id 0, full control), `Witness` (id 1), and `Active` (id ≥2, scoped to a bitmap of allowed `ContractType`s) [1](#0-0) .

When validating an `AccountPermissionUpdateContract`, the actuator's `checkPermission()` only verifies that any bit set in an Active permission's `operations` bitmap corresponds to a *currently valid* `ContractType` from `dynamicStore.getAvailableContractType()` — it does not exclude `AccountPermissionUpdateContract` itself from that bitmap: [2](#0-1) 

Separately, the test `checkActiveDefaultOperations` demonstrates that the *default* active-permission template explicitly excludes `AccountPermissionUpdateContract` from its bitmap — showing the maintainers recognize this contract type is special and should not normally be reachable from an Active key — yet nothing in `checkPermission()` enforces this exclusion for permissions explicitly configured by an owner: [3](#0-2) 

The transaction-level permission gate that decides whether a non-owner (`permissionId != 0`) signer is allowed to invoke a given contract type only checks the operations bitmap, with no special-case for `AccountPermissionUpdateContract`: [4](#0-3) [5](#0-4) 

Once such a transaction is validated as sufficiently signed under the Active permission, `execute()` unconditionally overwrites the account's Owner permission (and clears/rebuilds Active permissions) with whatever the caller supplied, with no check that the signer holds Owner-level authority: [6](#0-5) [7](#0-6) 

If any account (an exchange hot-wallet, multisig business account, custodial service, DApp treasury, etc.) is ever configured with an Active permission whose operations bitmap includes `AccountPermissionUpdateContract` — whether by an operator mistake, a business-logic template that grants broad operation sets, or a compromised/insider Active-key holder abusing legitimately-granted-but-supposedly-scoped access — that Active-key holder can broadcast a single `AccountPermissionUpdateContract` transaction (signed with `permissionId` set to their Active permission id) to replace the Owner permission's keys entirely, permanently locking out the true owner and seizing unrestricted control of the account (transfers, freezing/voting, further permission changes, etc.).

### Impact Explanation
This is a direct analog of the JupyterHub CVE: a permission tier intended to be scoped/limited (`admin:users` scope / TRON Active permission) is not actually prevented from bootstrapping itself to the fully privileged tier (`admin=True` / TRON Owner permission). Successful exploitation results in unauthorized account takeover — permanent loss of control of the victim account's Owner permission and, transitively, all funds and voting/staking rights controlled by that account. This satisfies the "concrete unauthorized account operation / theft or permanent freezing of funds" bar.

### Likelihood Explanation
Exploitation requires that some account already has an Active permission whose `operations` bitmap includes the `AccountPermissionUpdateContract` bit (bit corresponding to `ContractType.AccountPermissionUpdateContract`). The actuator's validation does nothing to prevent an owner (intentionally or by mistake, e.g. via tooling that sets broad/"all contract types" bitmaps) from creating such a permission, and once created, any holder of the corresponding Active key(s) meeting the threshold can perform the escalation with a single signed transaction — no special privileges beyond holding that Active key are needed. The main mitigating factor is that this requires a pre-existing misconfiguration (an Active permission with this bit set) rather than being exploitable from a completely default account state, similar to how `admin:users` is "already an extremely privileged scope" in the original advisory.

### Recommendation
In `AccountPermissionUpdateActuator.checkPermission()`, explicitly reject `Active` permissions whose `operations` bitmap includes the `AccountPermissionUpdateContract` bit, so that no Active-tier key can ever be used to modify the Owner (or other Active) permissions of the account — restricting `AccountPermissionUpdateContract` to Owner (`permissionId == 0`) only, consistent with how the default active-permission template already excludes it.

### Proof of Concept
1. Owner account `A` (holding Owner key `K_owner`) creates an Active permission `P_active` (id=2) with threshold 1 and a single key `K_active`, and sets `P_active.operations` to include the bit for `ContractType.AccountPermissionUpdateContract` (e.g., via a permission-update flow/tool that sets a broad "allow all" bitmap), then broadcasts this via `AccountPermissionUpdateActuator` signed by `K_owner`.
2. Holder of `K_active` crafts a new `AccountPermissionUpdateContract` for account `A` that sets a brand-new Owner permission containing only their own key `K_attacker`, sets `Contract.permission_id = 2`, and signs solely with `K_active`.
3. `TransactionCapsule.validateSignature`/`checkPermission` accepts the signature because `permission.getType() == Active` and the operations bitmap contains the `AccountPermissionUpdateContract` bit [8](#0-7) .
4. `AccountPermissionUpdateActuator.execute()` calls `account.updatePermissions(...)`, overwriting `A`'s Owner permission with the attacker-controlled key, permanently locking out `K_owner` [6](#0-5) .

Note: I was unable to fully verify the exact default value / bit layout returned by `DynamicPropertiesStore.getAvailableContractType()` (the store implementation file did not surface the constant in search), so I cannot confirm from code alone whether `AccountPermissionUpdateContract`'s bit is set to "available" by default at the dynamic-store level (independent of the Active-permission template check in `checkActiveDefaultOperations`). This does not affect the core finding — that `checkPermission()` has no explicit blacklist for `AccountPermissionUpdateContract` in Active permissions — but a full confirmation of the default bitmap value would require deeper inspection of `DynamicPropertiesStore`.

### Citations

**File:** protocol/src/main/protos/core/Tron.proto (L261-268)
```text
message Permission {
  enum PermissionType {
    Owner = 0;
    Witness = 1;
    Active = 2;
  }
  PermissionType type = 1;
  int32 id = 2; //Owner id=0, Witness id=1, Active id start by 2
```

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
