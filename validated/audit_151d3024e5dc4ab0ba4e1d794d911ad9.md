### Title
Instant, Unrestricted Owner-Controlled Permission Takeover with No Grace Period - (File: actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java)

### Summary
The `AccountPermissionUpdateActuator`, which handles `AccountPermissionUpdateContract` transactions broadcastable by any account holder, allows the `Owner` permission of a multi-sig-enabled account to instantly and unilaterally overwrite the entire `Owner`, `Witness`, and all `Active` permission sets (i.e., all "admin" keys/thresholds controlling that account) in a single transaction, with no delay, grace period, or opportunity for co-signers to react.

### Finding Description
`AccountPermissionUpdateActuator.execute()` directly calls `account.updatePermissions(...)` with the new `Owner`, `Witness`, and `Actives` permission lists taken straight from the submitted contract, and persists them immediately in the same transaction that alters them. [1](#0-0) 

`validate()` only checks structural correctness of the new permissions (key counts, weights, thresholds, operation bitmap, address distinctness) — it performs no check on whether the *previous* set of admin/active keys had any say in the timing of the change, and enforces no waiting/grace period before the new permission set takes effect. [2](#0-1) 

This is structurally the same bug class as the reported issue: a privileged "owner" role (here, the `Owner` permission holder of a TRON multi-sig account) can add, remove, or completely replace the set of "admin" keys (`Active` permissions, which govern which keys can execute which contract types) instantly and without restriction. If the keys backing the `Owner` permission are compromised or acting maliciously, an attacker can immediately strip out all legitimate `Active`/co-signer keys and install attacker-controlled keys in the same transaction — with zero opportunity for the account's other administrators to detect and react before the change takes effect, unlike the time-locked pattern referenced in the report.

### Impact Explanation
Any TRON account that has enabled multi-sign (`AllowMultiSign`) and relies on a threshold `Owner` permission composed of multiple keys (a common security control specifically intended to prevent single-key compromise from being catastrophic) can have its entire permission/admin structure rewritten in one atomic, unstoppable transaction the moment the `Owner` threshold is met by an attacker. This enables immediate unauthorized account takeover: legitimate `Active` keys (which gate execution of specific contract types, including TRC-20/TRX transfers) can be revoked and replaced in the same instant, with no window for the remaining co-signers to intervene, matching the "no measure to stop it" impact described in the report.

### Likelihood Explanation
Exploitation requires meeting the account's existing `Owner` permission threshold (e.g., compromising or colluding among enough `Owner` keys), which is the same precondition assumed by the original report ("once the owner itself gets hacked"). No other privilege, network position, or race condition is required — a single well-formed `AccountPermissionUpdateContract` transaction, broadcastable by anyone able to satisfy the owner threshold, is sufficient.

### Recommendation
Introduce a grace/time-lock period for `AccountPermissionUpdateContract` changes to `Owner` and `Active` permissions, analogous to the pattern in [Time Lock | Solidity by Example](https://solidity-by-example.org/app/time-lock/): require the new permission set to be queued and only become effective after a minimum delay, allowing existing key holders time to detect and counteract malicious or compromised-owner permission changes before they take effect. This delay should apply independently to changes affecting the `Owner` and `Active` permission sets.

### Proof of Concept
1. Attacker compromises/colludes to satisfy the current `Owner` permission threshold on a multi-sign-enabled account (`AccountStore` entry with `AllowMultiSign == 1`).
2. Attacker submits an `AccountPermissionUpdateContract` transaction via `AccountPermissionUpdateActuator`, setting `Owner`, `Actives` (and `Witness`, if applicable) to attacker-controlled keys only, excluding all previously legitimate co-signer keys.
3. `validate()` passes all structural checks (valid key counts/weights/threshold) since nothing prevents removing prior keys. [3](#0-2) 
4. `execute()` immediately persists the new permission set to `AccountStore`, taking full effect in the same block with no delay. [4](#0-3) 
5. The previous legitimate `Owner`/`Active` key holders instantly lose all control over the account with no opportunity to react.

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

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L148-229)
```java
  @Override
  public boolean validate() throws ContractValidateException {

    if (chainBaseManager == null) {
      throw new ContractValidateException(ActuatorConstant.STORE_NOT_EXIST);
    }

    if (this.any == null) {
      throw new ContractValidateException(ActuatorConstant.CONTRACT_NOT_EXIST);
    }

    AccountStore accountStore = chainBaseManager.getAccountStore();
    DynamicPropertiesStore dynamicStore = chainBaseManager.getDynamicPropertiesStore();

    if (dynamicStore.getAllowMultiSign() != 1) {
      throw new ContractValidateException("multi sign is not allowed, "
          + "need to be opened by the committee");
    }
    if (!this.any.is(AccountPermissionUpdateContract.class)) {
      throw new ContractValidateException(
          "contract type error,expected type [AccountPermissionUpdateContract],real type["
              + any.getClass() + "]");
    }
    final AccountPermissionUpdateContract accountPermissionUpdateContract;
    try {
      accountPermissionUpdateContract = any.unpack(AccountPermissionUpdateContract.class);
    } catch (InvalidProtocolBufferException e) {
      logger.debug(e.getMessage(), e);
      throw new ContractValidateException(e.getMessage());
    }
    byte[] ownerAddress = accountPermissionUpdateContract.getOwnerAddress().toByteArray();
    if (!DecodeUtil.addressValid(ownerAddress)) {
      throw new ContractValidateException("invalidate ownerAddress");
    }
    AccountCapsule accountCapsule = accountStore.get(ownerAddress);
    if (accountCapsule == null) {
      throw new ContractValidateException("ownerAddress account does not exist");
    }

    if (!accountPermissionUpdateContract.hasOwner()) {
      throw new ContractValidateException("owner permission is missed");
    }

    if (accountCapsule.getIsWitness()) {
      if (!accountPermissionUpdateContract.hasWitness()) {
        throw new ContractValidateException("witness permission is missed");
      }
    } else {
      if (accountPermissionUpdateContract.hasWitness()) {
        throw new ContractValidateException("account isn't witness can't set witness permission");
      }
    }

    if (accountPermissionUpdateContract.getActivesCount() == 0) {
      throw new ContractValidateException("active permission is missed");
    }
    if (accountPermissionUpdateContract.getActivesCount() > 8) {
      throw new ContractValidateException("active permission is too many");
    }

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
  }
```
