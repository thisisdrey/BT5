Confirmed: `DecodeUtil.addressValid` only checks length (21 bytes) and the network prefix byte; it does not reject the all-zero address body (i.e., prefix-byte + 20 zero bytes). [1](#0-0) 

This directly mirrors the reported bug class: an address that passes validation but corresponds to no known private key can be assigned to an "admin"-like governance role, permanently bricking control.

### Title
Missing zero-address rejection in `AccountPermissionUpdateActuator.checkPermission` allows an account owner to permanently and irrecoverably lock the Owner permission of their own account - (File: `actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java`)

### Summary
`AccountPermissionUpdateActuator` lets an account holder replace their `Owner`, `Witness`, and `Active` permissions via a signed `AccountPermissionUpdateContract`. Every key address in a permission is validated only with `DecodeUtil.addressValid`, which merely checks the address length and network prefix byte and never rejects the reserved all-zero address body. [2](#0-1) [1](#0-0) 

### Finding Description
`checkPermission` iterates over the keys of a permission, checking distinctness, `addressValid`, and positive weight, but does not exclude the zero address (prefix byte + 20 zero bytes) or other unspendable/known-unowned addresses. [3](#0-2) 
Because `AccountPermissionUpdateContract` requires an `Owner` permission on every submission (`hasOwner()` check), and `execute()` directly overwrites the account's permissions with `account.updatePermissions(...)` without any subsequent safety check, a transaction that sets the sole (or threshold-satisfying) `Owner` key(s) to the zero address will be accepted and applied. [4](#0-3) [5](#0-4) 
This is the direct analog of the reported Timelock issue: an "admin"-equivalent value (here, the Owner permission key controlling all subsequent `AccountPermissionUpdateContract` and privileged operations for the account) is assigned an address with no discoverable private key, permanently breaking the ability to further update permissions, freeze/unfreeze, vote, or otherwise administer the account.

### Impact Explanation
Once the `Owner` permission's controlling key(s) point to the zero address (or another address nobody controls) and the weight/threshold math is satisfied, no future transaction can obtain the required signature threshold for the `Owner` permission, since `Owner` is the only permission type that can authorize further `AccountPermissionUpdateContract` calls to fix itself. This permanently freezes the account's governance (multisig control) and by extension any balance or resources gated behind actions that require Owner-level authorization, matching the "permanent freezing of funds" impact bar.

### Likelihood Explanation
Reaching this path requires only a single, ordinarily-signed `AccountPermissionUpdateContract` transaction from the account owner (or any Active-permission key holder with sufficient authorized weight to modify permissions, depending on deployed active-permission scopes), with `AllowMultiSign` enabled on the network. No special privilege beyond normal transaction broadcasting is needed. The most likely trigger is accidental (a client mistakenly submitting an all-zero key, e.g., due to a wallet bug or bad address parsing), which is exactly the same failure mode described in the original report — assignment of a "null"/zero address to an admin-equivalent slot due to insufficient input validation.

### Recommendation
Add an explicit check in `checkPermission` (and any other actuator/path that accepts `Key.address` for permission updates) rejecting the all-zero address body, e.g.:
```java
if (Arrays.equals(key.getAddress().toByteArray(), ZERO_ADDRESS_BYTES)) {
  throw new ContractValidateException("key's address cannot be the zero address");
}
```
where `ZERO_ADDRESS_BYTES` is `{ addressPreFixByte, 0, 0, ..., 0 }` (21 bytes). Consider also validating that all listed key addresses correspond to accounts/addresses that exist or are otherwise plausible, at least for the `Owner` permission type, to reduce the risk of irrecoverable governance lockout.

### Proof of Concept
1. Attacker/account owner has `AllowMultiSign` enabled on chain.
2. They submit an `AccountPermissionUpdateContract` where the `Owner` permission has a single key with `address = <prefix_byte>+20 zero bytes` and `weight >= threshold`.
3. `AccountPermissionUpdateActuator.validate()` passes: `DecodeUtil.addressValid` only checks length/prefix, `checkPermission` finds `weightSum >= threshold`, and all other structural checks pass. [6](#0-5) 
4. `execute()` calls `account.updatePermissions(...)`, persisting the new Owner permission bound to the zero address. [7](#0-6) 
5. No subsequent transaction can produce a valid signature for the zero address, so the account can never again submit an authorized `AccountPermissionUpdateContract` (or any other Owner-gated operation), permanently locking account governance.

### Citations

**File:** common/src/main/java/org/tron/common/utils/DecodeUtil.java (L15-32)
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

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L95-122)
```java
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

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L187-189)
```java
    if (!accountPermissionUpdateContract.hasOwner()) {
      throw new ContractValidateException("owner permission is missed");
    }
```
