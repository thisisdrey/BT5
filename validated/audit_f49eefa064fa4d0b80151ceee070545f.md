## Analog Found

### Title
Missing zero-address check when setting Owner/Active permission keys allows permanent loss of account control - (File: `actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java`)

### Summary
The external report flags `DSAuth.setOwner` for accepting a zero address and thereby permanently losing contract control. The equivalent authority-setting path in java-tron is `AccountPermissionUpdateActuator`, which lets an account owner rewrite its own `Owner`/`Active`/`Witness` permissions (multi-sig keys and thresholds) via a signed `AccountPermissionUpdateContract`. The only address-format check applied to each permission key is `DecodeUtil.addressValid`, which — like the flagged Solidity code lacking a zero-address guard — never rejects the all-zero (black-hole) address.

### Finding Description
`AccountPermissionUpdateActuator.checkPermission` validates each `Key` in a permission only for format validity: [1](#0-0) 

This delegates to `DecodeUtil.addressValid`, which checks only length (21 bytes) and the network prefix byte, never checking whether the address is the reserved all-zero/black-hole address: [2](#0-1) 

`validate()` accepts the caller-supplied `ownerAddress` with the same format-only check and then applies the new `Owner` permission directly, with no additional restriction on the key set: [3](#0-2) 

Because the `Owner` permission is the top-level authority that itself governs future `AccountPermissionUpdateContract` transactions, a transaction that sets the sole (or threshold-satisfying) `Owner` permission key(s) to the all-zero address will pass validation and be persisted, since no private key exists for the zero address, this operation is equivalent to `setOwner(address(0))` in the referenced report: the account can never again satisfy its `Owner` permission threshold, permanently locking out any future permission changes, witness/vote management requiring owner authority, or asset/authority operations gated by that permission.

### Impact Explanation
This causes permanent, unrecoverable loss of authority over the account's own permission structure — functionally identical to the reported impact ("loss of contract control"). Because `Owner` permission also gates critical account operations (further `AccountPermissionUpdateContract`, and depending on configuration, other authority-based operations), an account bricking its own owner key set can permanently freeze administrative control of that account's funds/authority management.

### Likelihood Explanation
Reachable directly by any account holder broadcasting a single signed `AccountPermissionUpdateContract` transaction — no special privilege beyond controlling one's own account is required, matching the "unprivileged transaction broadcaster" reachability constraint. It is most likely to occur via user/tooling error (e.g., a malformed multisig setup script) rather than deliberate self-attack, but the absence of a black-hole/zero-address guard is a direct root-cause match to the reported bug class.

### Recommendation
In `AccountPermissionUpdateActuator.checkPermission` (and analogously wherever `DecodeUtil.addressValid` gates authority-defining addresses), add an explicit rejection of the reserved black-hole/zero address (`addressPreFixByte` followed by all-zero bytes) for any key used in the `Owner` permission, in addition to the existing weight/threshold checks, to prevent an account from permanently disabling its own owner authority.

### Proof of Concept
1. Attacker/account owner crafts an `AccountPermissionUpdateContract` where the `Owner` permission's key list contains only the all-zero address (`0x41` + 20 zero bytes) with weight ≥ threshold.
2. Submit the transaction signed by the current valid owner key(s) (satisfying the *current* permission, which is still intact at submission time).
3. `AccountPermissionUpdateActuator.validate()` → `checkPermission(owner)` passes because `DecodeUtil.addressValid` only checks length/prefix, not zero-value.
4. `execute()` persists the new `Owner` permission containing the unusable zero-address key.
5. From this block onward, no signature can ever satisfy the account's `Owner` permission threshold, permanently freezing that account's ability to update its own permissions. [4](#0-3)

### Citations

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

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L178-228)
```java
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
