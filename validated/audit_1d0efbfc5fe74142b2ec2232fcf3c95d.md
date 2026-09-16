### Title
`DecodeUtil.addressValid` does not reject the zero address, allowing `AccountPermissionUpdateActuator` to set an account's Owner permission to an unrecoverable key - ([File: actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java])

### Summary
`AccountPermissionUpdateActuator.checkPermission` / `validate` relies exclusively on `DecodeUtil.addressValid` to validate every key address used in a permission update, but that function only checks address length and the network prefix byte, never that the remaining 20 bytes are non-zero.

### Finding Description
`checkPermission` validates each `Key` in a submitted `Permission` (owner/witness/active) using `DecodeUtil.addressValid(key.getAddress().toByteArray())`: [1](#0-0) 

`DecodeUtil.addressValid` only checks that the address is 21 bytes long and starts with the correct prefix byte; it never checks that the address value itself is non-zero: [2](#0-1) 

As a result, a user submitting an `AccountPermissionUpdateContract` (broadcastable, unprivileged transaction) can set the account's `Owner` permission key list to contain only the "zero" address (prefix byte + 20 zero bytes) with sufficient weight to satisfy the permission threshold: [3](#0-2) 

This directly mirrors the reported bug class: `LibOwnable._setAdmin` accepting `address(0)` as admin with no check, silently bricking admin-gated functionality. Here, the "admin" analog is the account's `Owner` `Permission`, which is required to authorize privileged operations such as future `AccountPermissionUpdateContract` transactions (permission changes) for that same account.

### Impact Explanation
Because nobody can ever produce a valid signature for the zero address, an account whose `Owner` permission threshold can only be met using the zero-address key becomes permanently unable to execute further `AccountPermissionUpdateContract` transactions, and any other owner-permission-gated operation is permanently locked for that account. This is a permanent, irreversible loss of control analogous to the reported `_setAdmin(address(0))` issue — the account is effectively "bricked" with respect to any operation requiring Owner-permission signatures.

### Likelihood Explanation
Reaching this path only requires broadcasting a single, syntactically valid `AccountPermissionUpdateContract` transaction signed by the account's current Owner permission (multi-sign must be enabled, i.e., `AllowMultiSign == 1`), which is a normal, unprivileged operation available to any account holder. No special privileges, race conditions, or additional preconditions beyond normal multisig usage are needed.

### Recommendation
Add an explicit check rejecting the zero address (and any other all-zero/invalid-value address) inside `DecodeUtil.addressValid`, or add a dedicated check in `AccountPermissionUpdateActuator.checkPermission` (and other actuators/paths that rely on `addressValid` for permission/ownership-critical fields) to reject a key address equal to the zero address before allowing it to be persisted as part of a `Permission`.

### Proof of Concept
1. Attacker (or a careless account owner) has an account with `AllowMultiSign` enabled.
2. They submit an `AccountPermissionUpdateContract` transaction whose `Owner` permission contains a single `Key` with `address = 0x41 + 20×0x00` (valid length/prefix, but the zero value) and `weight >= threshold`.
3. `AccountPermissionUpdateActuator.validate()` calls `checkPermission(owner)`, which calls `DecodeUtil.addressValid(key.getAddress())` — this returns `true` because only length and prefix are checked.
4. `execute()` persists this permission via `account.updatePermissions(...)`.
5. From this point on, no valid signature can ever satisfy the account's Owner permission threshold, permanently locking out any further Owner-permission-gated transactions (including attempts to fix the permission via another `AccountPermissionUpdateContract`). [4](#0-3)

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L34-69)
```java
  @Override
  public boolean execute(Object object) throws ContractExeException {
    TransactionResultCapsule result = (TransactionResultCapsule) object;
    if (Objects.isNull(result)) {
      throw new RuntimeException(ActuatorConstant.TX_RESULT_NULL);
    }

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

      result.setStatus(fee, code.SUCESS);
    } catch (BalanceInsufficientException | InvalidProtocolBufferException e) {
      logger.debug(e.getMessage(), e);
      result.setStatus(fee, code.FAILED);
      throw new ContractExeException(e.getMessage());
    }

    return true;
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L105-111)
```java
    for (Key key : permission.getKeysList()) {
      if (!DecodeUtil.addressValid(key.getAddress().toByteArray())) {
        throw new ContractValidateException("key is not a validate address");
      }
      if (key.getWeight() <= 0) {
        throw new ContractValidateException("key's weight should be greater than 0");
      }
```

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L148-221)
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
```

**File:** common/src/main/java/org/tron/common/utils/DecodeUtil.java (L15-33)
```java
  public static boolean addressValid(byte[] address) {
    if (ArrayUtils.isEmpty(address)) {
      logger.warn("Warning: Address is empty !!");
      return false;
    }
    if (address.length != ADDRESS_SIZE / 2) {
      logger.warn(
          "Warning: Address length need " + ADDRESS_SIZE + " but " + address.length
              + " !!");
      return false;
    }

    if (address[0] != addressPreFixByte) {
      logger.warn("Warning: Address need prefix with " + addressPreFixByte + " but "
          + address[0] + " !!");
      return false;
    }
    return true;
  }
```
