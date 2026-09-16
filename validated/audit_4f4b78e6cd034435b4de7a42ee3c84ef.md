### Title
Unbacked owner-permission key update permanently locks TRX/TRC funds - ([File: actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java])

### Summary
`AccountPermissionUpdateActuator` lets the account owner replace the `Owner`, `Witness` and `Active` permissions of an account in a single signed `AccountPermissionUpdateContract`. The validation logic only checks structural properties of the submitted permission set (key count, address format, weight positivity, threshold reachability, operation bitmap), but never verifies that the account issuing the update retains any usable/controllable key afterward, nor does it keep any rollback/backup of the previous permission set once `execute()` overwrites it in the `AccountStore`. This mirrors the Fireblocks incident, where an authorized operation (key deletion) executed without any safety net (backup) rendered ETH permanently inaccessible.

### Finding Description
`validate()` calls `checkPermission()` for the `Owner`, `Witness`, and each `Active` permission and only enforces:
- key count bounds and distinct addresses, [1](#0-0) 
- non-zero threshold and weight, and that the sum of weights is not less than the threshold, [2](#0-1) 
- valid `ContractType` bits for `Active` permission operations. [3](#0-2) 

None of these checks confirm that the owner (the signer submitting the transaction) still possesses a private key matching any address left in the new `Owner` permission. `execute()` then unconditionally overwrites the account's permission set with `account.updatePermissions(...)` and persists it, with no versioning, staged rollout, or recovery mechanism. [4](#0-3) 

Because the update is a single atomic, irreversible on-chain state transition (once the old `Owner` permission is replaced, the old key(s) no longer authorize the account), a single erroneous or truncated transaction — analogous to Fireblocks deleting the operative key without a backup — permanently and unrecoverably locks the account's entire balance and its right to ever issue another `AccountPermissionUpdateContract`, `TransferContract`, or any other owner-gated operation.

### Impact Explanation
If the new `Owner` permission's key set does not correspond to any private key the operator actually controls (e.g., an operational mistake analogous to Fireblocks' key deletion, a corrupted signer configuration, or a typo'd public key hash), the account becomes permanently unable to authorize any further transaction. Since the `Owner` permission also gates the ability to change permissions again, the lock-out is irreversible — funds are frozen forever with no protocol-level recovery path. This meets the "permanent freezing of funds" impact bar.

### Likelihood Explanation
The path is reachable by any account holder issuing a single signed `AccountPermissionUpdateContract` (subject to `AllowMultiSign` being enabled), with no special privilege required beyond owning the account being modified. Because the actuator's validation is purely structural and does not sanity check key control, an operational error in key generation/management by an exchange, custodian, or automated signer service (the exact scenario in the Fireblocks report) is sufficient to trigger irreversible loss, without any attacker action needed.

### Recommendation
Add a safety mechanism to `AccountPermissionUpdateActuator`/`checkPermission()` such as: requiring a challenge-response or two-step commit (propose + confirm signed by the new key) before an `Owner` permission replacement takes effect, or requiring the transaction itself be additionally co-signed with a currently valid key that will remain effective post-update, so that self-lockout cannot occur from a single unverified permission swap.

### Proof of Concept
1. Create/own an account with a multisig `Owner` permission (`AllowMultiSign=1`).
2. Submit an `AccountPermissionUpdateContract` (signed by the current valid owner key(s)) whose new `Owner.keys` list contains only addresses for which no participant retains the private key (structurally valid — passes `checkPermission()`'s address-format/threshold/weight checks in `AccountPermissionUpdateActuator.checkPermission` lines 71-122).
3. `execute()` calls `account.updatePermissions(...)` and persists the new permission set unconditionally (`AccountPermissionUpdateActuator.execute`, lines 44-61).
4. No subsequent transaction from this account can ever be authorized again — its TRX/TRC-20/staked balance is permanently frozen, exactly analogous to the Fireblocks incident where deleting the operative signing key without backup led to permanent loss of the underlying ETH.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L44-61)
```java
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

      result.setStatus(fee, code.SUCESS);
```

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L71-82)
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
```

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L83-122)
```java
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
