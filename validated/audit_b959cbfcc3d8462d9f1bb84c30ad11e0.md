### Title
Active-permission holder can self-escalate to full Owner control via AccountPermissionUpdateContract - (File: actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java)

### Summary
`AccountPermissionUpdateActuator` allows completely rewriting an account's Owner, Witness, and Active permissions in a single transaction, but neither `validate()` nor `checkPermission()` verifies that the *authorizing* permission used to sign the transaction was actually the Owner permission. Instead, authorization is delegated to the generic transaction-signature path, which only requires that if a non-default `permissionId` is used, it be an Active-type permission whose `operations` bitmap includes the `AccountPermissionUpdateContract` bit.

### Finding Description
`TransactionCapsule.checkPermission()` (the routine invoked from both `validateSignature` and `addSign`) enforces only this rule for non-owner permission IDs: [1](#0-0) 

It does not special-case `AccountPermissionUpdateContract` to require the Owner permission (id 0). As long as an Active permission's 32-byte `operations` bitmap has the corresponding bit set, any key(s) satisfying that Active permission's threshold can sign an `AccountPermissionUpdateContract` transaction.

Once such a transaction is authorized, `AccountPermissionUpdateActuator.validate()`/`checkPermission()` only validates *structural* correctness of the new permissions (key count ≤ `getTotalSignNum()`, non-zero threshold, distinct/valid addresses, weight sum ≥ threshold, valid contract-type bits) — it never compares the new Owner/Active permission structure against the *identity or weight* of whoever authorized the change: [2](#0-1) [3](#0-2) 

`execute()` then unconditionally calls `account.updatePermissions(...)`, overwriting Owner, Witness, and all Active permissions with attacker-supplied values: [4](#0-3) 

This mirrors the GitLab bug class: a holder of a narrowly-scoped/limited permission (an Active permission meant only for specific contract types) is able to perform an operation that grants Owner-level privilege (rewriting the Owner permission itself), because the permission-chaining/authorization check for the *sensitive* action (permission management) is not tied to actually holding Owner authority — only to having the correct bit set in an Active permission's operations bitmap.

### Impact Explanation
Any account owner who grants an Active permission (e.g., to a co-signer, employee, dApp, or automated signer) that includes the `AccountPermissionUpdateContract` bit in its `operations` field — even unintentionally, since this is one of 256 contract-type bits an operator might toggle on when constructing a broad "admin-lite" Active key — gives that key holder the ability to unilaterally replace the account's Owner permission with a permission structure fully controlled by the attacker (e.g., threshold 1, single attacker-controlled key). This permanently locks out the legitimate owner and gives the attacker sole control of the account, including its balance, TRX/TRC10 assets, staked/frozen resources, and voting rights — a full unauthorized account takeover.

### Likelihood Explanation
Exploitation requires only a single broadcastable transaction from a key that already holds an Active permission whose bitmap includes the `AccountPermissionUpdateContract` bit — no witness/committee/SR privileges are needed. Since account owners configure Active-permission operation bitmaps themselves (via wallets/CLI/servlets such as `AccountPermissionUpdateServlet`), and the actuator/validation code performs no additional restriction preventing an Active key from including this particular contract type, any misconfiguration (or intentionally malicious delegation, e.g., a business granting a "limited" active key to a partner/service and including this bit by oversight) directly yields privilege escalation with no additional signatures or approvals needed.

### Recommendation
In `AccountPermissionUpdateActuator.validate()` (and/or in `TransactionCapsule.checkPermission`), explicitly reject `AccountPermissionUpdateContract` transactions signed with any `permissionId != 0` (i.e., require the Owner permission for permission-management operations), or, at minimum, disallow the `AccountPermissionUpdateContract` type bit from ever being set in an Active permission's `operations` bitmap during `checkPermission()`'s bitmap validation.

### Proof of Concept
1. Account `OWNER_ADDRESS` sets up permissions via `AccountPermissionUpdateContract`: Owner permission = {ownerKey, threshold 1}; Active permission id=2 = {threshold 1, key=`delegateKey`, operations bitmap with the `AccountPermissionUpdateContract` bit set (along with whatever other operations were intended, e.g. transfers)} — a realistic "limited admin" delegation.
2. Attacker controlling `delegateKey` crafts a new `AccountPermissionUpdateContract` transaction with `permission_id = 2` (the Active permission) as `contract.getPermissionId()`, setting:
   - `owner` = new Owner permission with threshold 1 and a single attacker-controlled key.
   - `actives` = attacker-controlled Active permission(s).
3. Attacker signs only with `delegateKey`. `TransactionCapsule.checkPermission()` sees `permissionId=2`, confirms it's Active type, and confirms the `AccountPermissionUpdateContract` bit is set in its operations bitmap — signature accepted.
4. `AccountPermissionUpdateActuator.validate()` passes because the new Owner/Active permissions are structurally valid (non-zero threshold, valid keys, weight sum ≥ threshold) — it never checks that the transaction was authorized by the previous Owner permission.
5. `execute()` calls `account.updatePermissions(...)`, replacing the account's Owner permission with the attacker's. The original owner is now permanently locked out, and the attacker has sole Owner control of the account.

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

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L71-146)
```java
  private boolean checkPermission(Permission permission) throws ContractValidateException {
    DynamicPropertiesStore dynamicStore = chainBaseManager.getDynamicPropertiesStore();
    if (permission.getKeysCount() > dynamicStore.getTotalSignNum()) {
      throw new ContractValidateException("number of keys in permission should not be greater "
          + "than " + dynamicStore.getTotalSignNum());
    }
    if (permission.getKeysCount() == 0) {
      throw new ContractValidateException("key's count should be greater than 0");
    }
    if (permission.getType() == PermissionType.Witness && permission.getKeysCount() != 1) {
      throw new ContractValidateException("Witness permission's key count should be 1");
    }
    if (permission.getThreshold() <= 0) {
      throw new ContractValidateException("permission's threshold should be greater than 0");
    }
    String name = permission.getPermissionName();
    if (!StringUtils.isEmpty(name) && name.length() > 32) {
      throw new ContractValidateException("permission's name is too long");
    }
    //check owner name ?
    if (permission.getParentId() != 0) {
      throw new ContractValidateException("permission's parent should be owner");
    }

    long weightSum = 0;
    List<ByteString> addressList = permission.getKeysList()
        .stream()
        .map(x -> x.getAddress())
        .distinct()
        .collect(toList());
    if (addressList.size() != permission.getKeysList().size()) {
      throw new ContractValidateException(
          "address should be distinct in permission " + permission.getType());
    }
    for (Key key : permission.getKeysList()) {
      if (!DecodeUtil.addressValid(key.getAddress().toByteArray())) {
        throw new ContractValidateException("key is not a validate address");
      }
      if (key.getWeight() <= 0) {
        throw new ContractValidateException("key's weight should be greater than 0");
      }
      try {
        weightSum = addExact(weightSum, key.getWeight());
      } catch (ArithmeticException e) {
        throw new ContractValidateException(e.getMessage());
      }
    }
    if (weightSum < permission.getThreshold()) {
      throw new ContractValidateException(
          "sum of all key's weight should not be less than threshold in permission " + permission
              .getType());
    }

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

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L207-228)
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
    return true;
```
