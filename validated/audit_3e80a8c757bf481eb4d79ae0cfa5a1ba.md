### Title
Active-permission key with only the `AccountPermissionUpdateContract` operation bit set can rewrite Owner/Witness/Active permissions to any scope, escalating to full account control - (File: actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java)

### Summary
`AccountPermissionUpdateActuator.validate()`/`checkPermission()` only checks that a newly proposed `Owner`, `Witness`, or `Active` permission is *structurally* valid (key count, threshold, weight sum, and that `operations` bits are a subset of the chain-wide `getAvailableContractType()` bitmap). It never compares the requested new permissions against the scope of the permission that actually authorized the transaction (`contract.getPermissionId()`). Consequently, an account owner who grants a scoped Active key only the single operation bit for `AccountPermissionUpdateContract` (contract type 46) inadvertently — or an attacker who otherwise obtains signing rights under such a key — can use that narrow-scope key to replace the account's entire `Owner` permission (new keys/threshold/weight) and every `Active` permission (including granting itself full `operations` coverage across all contract types), fully escalating from a limited role to unrestricted account control. This mirrors the LiteLLM analog: a role-restricted principal can mint/alter permission grants without the system verifying those grants stay within the principal's own authorized scope.

### Finding Description
The signing-side check, implemented in `TransactionCapsule.checkPermission()` / `WalletUtil.checkPermissionOperations()` and `TransactionUtil.checkPermissionOperations()`, only verifies that the contract type being executed (`AccountPermissionUpdateContract`, value 46) is present in the *signer's* `operations` bitmap: [1](#0-0) 

Once that single-bit check passes, execution proceeds to `AccountPermissionUpdateActuator.validate()`, whose `checkPermission()` helper validates the *proposed new* permission purely on its own internal consistency (key count/threshold/weight, name length, parentId) and, for Active permissions, that the requested `operations` bitmap is a subset of the entire chain's `getAvailableContractType()` — not a subset of the signer's own current permission's `operations`: [2](#0-1) 

`execute()` then calls `AccountCapsule.updatePermissions()`, which unconditionally overwrites the Owner permission, the Witness permission, and clears/replaces all Active permissions with whatever was supplied in the contract: [3](#0-2) 

There is no code path anywhere in `validate()`/`execute()` that fetches the permission identified by `contract.getPermissionId()` (the one that actually authorized this specific transaction) and enforces that the new `Owner`/`Active` permissions being written cannot exceed that signer's own threshold, weight, key set, or operations scope. A limited Active key — granted by the account owner solely for the purpose of calling `AccountPermissionUpdateContract` (e.g., to enable routine key rotation) — can therefore unilaterally rewrite the Owner permission to add its own key with a controlling weight, and rewrite every Active permission to grant itself unrestricted `operations` (all bits allowed by `getAvailableContractType()`), achieving full "Owner"-equivalent control of the target account.

### Impact Explanation
Full account takeover: an Active key intended for a narrow, single-purpose delegation can convert itself into (or install a new) Owner-level key, or grant an arbitrary Active permission unrestricted operations across all contract types (transfers, freezes, exchange operations, delegation, etc.). This is unauthorized privilege escalation leading to theft of funds and full compromise of the target account, matching the "unauthorized account operation / theft of funds" impact bar.

### Likelihood Explanation
Requires only that the account owner has previously enabled the `AccountPermissionUpdateContract` operation bit (46) on some Active permission for a given key — a normal multisig-management pattern users may adopt (e.g., to allow a delegate to rotate keys). No other privileged access or bug is needed beyond controlling that single Active key's private key; the actuator itself performs no scope containment, so the escalation is a guaranteed effect of the missing check rather than a probabilistic exploit.

### Recommendation
In `AccountPermissionUpdateActuator.validate()`, resolve the permission referenced by `contract.getPermissionId()` from the current `AccountCapsule` and enforce that the proposed new `Owner`/`Witness`/`Active` permissions cannot grant a scope, threshold, or `operations` bitmap broader than that signing permission (e.g., require the signer's permission to be Owner (`permissionId == 0`) before allowing modification of the Owner permission or expansion of `operations` beyond the signer's own bitmap).

### Proof of Concept
1. Account `A` (Owner) creates Active permission `P2` for delegate key `D`, granting `P2` only the `operations` bit for `AccountPermissionUpdateContract` (id 46) — intended solely to let `D` rotate other Active keys.
2. `D` signs an `AccountPermissionUpdateContract` transaction with `permission_id = 2`. `TransactionCapsule.checkPermission()` passes because bit 46 is set in `P2.operations`.
3. The contract's payload sets a new `Owner` permission containing `D`'s own address with threshold satisfied by `D` alone, and a new Active permission for `D` with `operations` set to the full `getAvailableContractType()` bitmap.
4. `AccountPermissionUpdateActuator.checkPermission()` accepts this because it only validates structural constraints and that requested operation bits are within the chain-wide available set — never comparing against `P2`'s original scope.
5. `execute()` calls `updatePermissions()`, overwriting Owner and Active permissions as requested; `D` now controls account `A` at Owner level despite being originally granted only a single narrow operation.

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
