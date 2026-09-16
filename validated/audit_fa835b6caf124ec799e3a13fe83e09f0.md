### Title
Single-step, irrevocable account permission (owner/witness/active) replacement via `AccountPermissionUpdateContract` - (File: `actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java`)

### Summary
The `AccountPermissionUpdateContract` mechanism lets any account holder replace its `owner`, `witness`, and `active` permission sets (i.e., re-assign which keys/addresses control the account) in a single atomic transaction, with no propose-then-claim/two-step confirmation. This mirrors the reported Knox pattern of single-step privileged-role transfer: an admin-equivalent operation (changing who controls an account) is committed immediately based only on structural validation, without any acknowledgment step from the newly designated key holders.

### Finding Description
`AccountPermissionUpdateActuator.execute()` directly overwrites the account's permissions from the submitted contract: [1](#0-0) 

The `validate()`/`checkPermission()` logic only checks structural properties of the new permission — key count, threshold, weight sums, address format via `DecodeUtil.addressValid`, and operation bitmap validity: [2](#0-1) [3](#0-2) 

`DecodeUtil.addressValid` only checks length (42 hex chars / 21 bytes) and the TRON address prefix byte — it does not verify the address corresponds to a key whose private key is known/reachable, nor does it require any signature or acknowledgment from the new key holder(s): [4](#0-3) 

Because the change is applied to `Owner`/`Active`/`Witness` permissions in one step (`account.updatePermissions(...)`), any mistake in specifying the new owner key set — a typo'd address, an address whose private key is unavailable, or a permission whose threshold/weight combination makes it practically unsatisfiable (e.g., distributing weight such that no achievable signer combination reaches the threshold, though `checkPermission` does verify `weightSum >= threshold` numerically, it cannot verify the signer actually controls those keys) — permanently and irrevocably locks the account exactly as described in the reported issue: single-step ownership/permission change with no recovery path.

### Impact Explanation
If the transaction that updates `owner_address`'s `Owner` permission (or `Active`/`Witness` permission) specifies keys that the sender does not truly control (mistyped address, wrong key, or a byte-valid but uncontrolled address), the account becomes permanently unable to authorize any further `AccountPermissionUpdateContract` (or any other) transactions from that account, since the owner permission — the only permission type capable of re-issuing `AccountPermissionUpdateContract` — has been irrevocably replaced. This results in permanent loss of control over the account and any assets/TRX/TRC10 tokens held or delegated by it, matching the "permanent freezing of funds" / "unauthorized loss of account operation" impact bar.

### Likelihood Explanation
This requires only a legitimately signed `AccountPermissionUpdateContract` transaction from the account owner — no special privilege beyond normal account signing capability is needed to trigger the loss, and no malicious counterparty is required: an honest mistake by any account holder (unprivileged transaction broadcaster) suffices. `checkPermission` performs no economic or possession check that the party executing the update actually controls the newly designated keys, matching the report's described root cause (no two-step approve/claim to confirm the new party can act with the new role) exactly.

### Recommendation
Introduce a two-step confirmation flow for `AccountPermissionUpdateContract`, analogous to approve/claim ownership patterns: the account first proposes new `Owner`/`Active`/`Witness` permission sets, and the change only takes effect once each newly introduced key set (or at least the new owner-permission signer) confirms possession by submitting a signed acknowledgment transaction from the new key(s). Alternatively, provide a bounded time-locked pending-permission state with the ability to cancel/revert to the previous permission set before finalization, preventing irrecoverable lockout from a single mistaken transaction.

### Proof of Concept
1. Account `A` (with default owner/active permissions pointing to `A`) sends an `AccountPermissionUpdateContract` transaction setting a new `Owner` permission whose sole key is address `X` (attacker-typo'd or otherwise uncontrolled address), satisfying all structural checks in `checkPermission` (valid length/prefix via `DecodeUtil.addressValid`, `threshold <= weightSum`, distinct addresses, etc.).
2. `AccountPermissionUpdateActuator.execute()` calls `account.updatePermissions(...)` and commits the new permission set in the same transaction — no acknowledgment from `X` is solicited or required. [5](#0-4) 
3. Because nobody controls the private key for `X`, account `A` can never again sign a transaction with the required Owner-permission weight, permanently losing the ability to submit `AccountPermissionUpdateContract`, `TransferContract`, or any other contract requiring Owner/Active authorization from that account — resulting in permanent loss of control and effectively frozen funds/assets held by `A`.

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

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L71-122)
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
```

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L148-228)
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
