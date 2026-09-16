Based on my investigation, the strongest reachable analog in java-tron to the Keycloak FGAP v2 bug class ("a scoped delegate's action to create/configure a resource bypasses the authorization boundary of that delegate, letting it affect a resource/scope it doesn't control") is the permission-scope escalation in `AccountPermissionUpdateActuator`.

### Title
Active-permission signer can rewrite Owner and unrelated Active permissions via AccountPermissionUpdateContract, bypassing multisig scope boundaries - (File: `actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java`)

### Summary
TRON's account multisig model lets an account's Owner grant a scoped `Active` permission (a "sub-administrator" key, analogous to Keycloak's FGAP-scoped admin) with an `operations` bitmap limiting it to specific contract types. If `AccountPermissionUpdateContract` is included in that bitmap, the actuator that processes such a transaction never checks that the signing permission's authority is bounded to modifying only itself — it accepts and fully overwrites the Owner permission and every Active permission slot in one call.

### Finding Description
`AccountPermissionUpdateActuator.validate()` only performs structural validation of each submitted `Permission` (key count, distinct addresses, weight sums, threshold, name length, and — for Active permissions — that the requested `operations` bitmap only contains bits present in `dynamicStore.getAvailableContractType()`): [1](#0-0) 
Nowhere does `checkPermission()` or `validate()` compare the *acting* permission (the one identified by `contract.getPermissionId()`, whose signature actually authorized the transaction) against the *target* permissions being replaced (`owner`, `witness`, and every entry in `actives`). The authorization check for who may broadcast this contract type at all is performed earlier and separately, in `TransactionCapsule.validateSignature`/`checkPermission`, which only verifies that the signing permission's own `operations` bitmap has the `AccountPermissionUpdateContract` bit set — it does not restrict what that permission is allowed to *rewrite*: [2](#0-1) 
`execute()` then unconditionally calls `account.updatePermissions(owner, witness, actives)`, replacing the entire permission set of the account with attacker-controlled data in a single atomic operation: [3](#0-2) 
Because `getAvailableContractType()` includes `AccountPermissionUpdateContract` as a normal, grantable contract type (it is only excluded from the *default* auto-generated Active permission, not blocked from being explicitly granted — see the exclusion in `checkActiveDefaultOperations`): [4](#0-3) 
a limited/scoped Active-permission key that is granted this single operation bit inherits full account takeover capability: it can add its own address (or any address) to the Owner permission's key list with a low threshold, remove other legitimate signers from Owner/Active, or grant itself unrestricted `operations` in a new Active permission — none of which is checked against what the acting permission was actually delegated to do.

### Impact Explanation
This is the multisig analog of the Keycloak bug: a sub-scoped signer entrusted with a narrow capability (e.g., only allowed to sign `AccountPermissionUpdateContract` plus a couple of routine contract types) can escalate to full control of the account — including its Owner permission, which controls fund transfers, resource freezing, and voting. This results in concrete unauthorized account takeover and potential theft of funds, satisfying the High/Critical impact bar (unauthorized account operation / theft of funds).

### Likelihood Explanation
Exploitation requires the account owner to have granted the `AccountPermissionUpdateContract` bit to a non-Owner Active permission — a configuration TRON's own default-permission generation deliberately avoids (as shown by the exclusion in the test above), indicating awareness that this bit is dangerous to delegate. Given that any account owner (multisig wallet, exchange custody wallet, DAO treasury) could mistakenly or be socially-engineered into granting this bit to a "limited" operator key, and the actuator provides zero additional defense-in-depth once that bit is set, the likelihood is non-trivial for any deployment using TRON's multisig delegation feature for operational key separation.

### Recommendation
In `AccountPermissionUpdateActuator.validate()`, add a scope check that ties the acting permission (`contract.getPermissionId()`) to the extent of permitted changes: e.g., disallow an Active-permission-signed `AccountPermissionUpdateContract` from modifying the Owner permission or any Active permission other than its own slot, unless the transaction is signed with the Owner permission (`permission_id == 0`). Alternatively, treat `AccountPermissionUpdateContract` as non-grantable to Active permissions at the protocol level (reject it in the `operations` bitmap validation in `checkPermission`), consistent with how it is already excluded from default-generated Active permissions.

### Proof of Concept
1. Owner account `A` creates an Active permission `P2` with `operations` bitmap that includes bit for `TransferContract` and bit for `AccountPermissionUpdateContract`, assigns it to delegate key `K` with threshold 1 (a common "limited operator" configuration for exchange hot-wallet automation).
2. `K` broadcasts an `AccountPermissionUpdateContract` transaction with `permission_id = 2` (referencing `P2`), setting:
   - `owner` permission: keys = `[K]`, threshold = 1 (previously required 2-of-3 with other custodians)
   - `actives`: a single Active permission granting `K` all contract types
3. `AccountPermissionUpdateActuator.validate()` passes because each submitted `Permission` is structurally valid (per `checkPermission()`), and `TransactionCapsule.checkPermission()` only confirms `P2`'s bitmap contains the `AccountPermissionUpdateContract` bit — it never checks that `K` is authorized to alter the Owner permission or supersede other Active permissions.
4. `execute()` calls `account.updatePermissions(...)`, replacing Owner and Active permissions account-wide.
5. `K` now solely and fully controls account `A`, including its full TRX/TRC10/TRC20 balances and freeze/vote rights, despite having originally been scoped only to sign transfers.

### Citations

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

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L208-227)
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
    for (Permission permission : actives) {
      if (permission.getType() != PermissionType.Active) {
        throw new ContractValidateException("active permission type is error");
      }
      checkPermission(permission);
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
