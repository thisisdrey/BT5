Confirmed: `DecodeUtil.addressValid` only validates address length and the network prefix byte, and never rejects the all-zero address body [1](#0-0) . This validator is reused by `AccountPermissionUpdateActuator.checkPermission` to validate every `Key.getAddress()` in an owner/active/witness permission before it's persisted [2](#0-1) .

### Title
Owner permission can be set to an unrecoverable zero address in `AccountPermissionUpdateActuator`, permanently freezing an account - (File: `actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java`)

### Summary
`AccountPermissionUpdateContract` lets any account holder replace their account's `Owner`/`Active`/`Witness` permissions with new keys. `checkPermission()` validates key addresses only through `DecodeUtil.addressValid`, which checks address length and the chain prefix byte, but does not check that the address body is non-zero [3](#0-2) . This is analogous to the reported Solidity bug: an ownership-transfer style function accepts `address(0)` as a valid new controller, permanently disabling privileged control.

### Finding Description
`execute()` unconditionally calls `account.updatePermissions(...)` with whatever `Owner`/`Active`/`Witness` permissions passed `validate()` [4](#0-3) . The only address sanity check performed in `checkPermission` is `DecodeUtil.addressValid(key.getAddress().toByteArray())` [5](#0-4) , which — as shown in `DecodeUtil.addressValid` — accepts any 21-byte value with the correct prefix byte, including an address whose remaining 20 bytes are all zero [1](#0-0) . Because the private key for such an address is unknown/unattainable, setting the `Owner` permission's sole (or threshold-satisfying) key to this zero-body address makes the account's owner permission un-signable going forward, exactly mirroring the external report's root cause (an "ownership" field accepting `address(0)` with no non-zero check).

### Impact Explanation
Once the `Owner` permission of an account is updated to reference only unreachable key(s), that account can never again pass `AccountPermissionUpdateActuator.validate/execute` (since owner-permission transactions require signatures satisfying the current owner threshold, verified via `TransactionCapsule.validateSignature`/`checkWeight` against the stored `Permission` [6](#0-5) ), nor can it change its Active permissions, TRX/TRC10 balances still held by the account, delegated resources, or witness settings tied to owner authority. This is a permanent freezing-of-funds condition equivalent in class to the audited Vault issue, though the trigger here requires the account's own owner-permission signature.

### Likelihood Explanation
Reaching this path only requires a signed `AccountPermissionUpdateContract` transaction from an unprivileged account holder, with `AllowMultiSign` enabled on chain (a standard, already-active feature gate) [7](#0-6) . No committee/SR privilege, mocked path, or auxiliary contract is needed — any user can construct this transaction themselves (accidentally or via a malicious dApp/wallet tricking them into signing it), so likelihood of occurrence in the wild is non-trivial even though it is self-directed rather than exploited by a third party against a victim's will.

### Recommendation
In `AccountPermissionUpdateActuator.checkPermission`, reject keys whose address body is entirely zero (or equals the chain's designated blackhole/zero address) for `Owner`/`Active`/`Witness` permissions, in addition to the existing `DecodeUtil.addressValid` check, mirroring how `resetBlackholeAccountPermission` intentionally uses the zero address only for the special-cased blackhole account [8](#0-7) .

### Proof of Concept
1. Enable multisig (`AllowMultiSign=1`), which is standard on public networks.
2. From account `A`, submit an `AccountPermissionUpdateContract` where `owner.keys` contains a single key with address `410000000000000000000000000000000000000000` (zero body, correct prefix) and `weight >= threshold`.
3. `checkPermission` passes because `DecodeUtil.addressValid` only checks length/prefix [1](#0-0) ; `execute()` persists this as the new owner permission.
4. Account `A` can no longer produce a valid signature satisfying the new owner threshold (nobody controls the zero-body key), permanently locking owner-level operations and any funds/resources gated by them.

### Citations

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

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L44-59)
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
```

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L71-117)
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
```

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L162-165)
```java
    if (dynamicStore.getAllowMultiSign() != 1) {
      throw new ContractValidateException("multi sign is not allowed, "
          + "need to be opened by the committee");
    }
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

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L388-399)
```java
  private void resetBlackholeAccountPermission() {
    AccountCapsule blackholeAccount = getAccountStore().getBlackhole();

    byte[] zeroAddress = new byte[21];
    zeroAddress[0] = Wallet.getAddressPreFixByte();
    Permission owner = AccountCapsule
        .createDefaultOwnerPermission(ByteString.copyFrom(zeroAddress));
    blackholeAccount.updatePermissions(owner, null, null);
    getAccountStore().put(blackholeAccount.getAddress().toByteArray(), blackholeAccount);

    getDynamicPropertiesStore().saveSetBlackholePermission(1);
  }
```
