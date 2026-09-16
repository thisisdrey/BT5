Confirmed: `DecodeUtil.addressValid` only checks that the address is 21 bytes and starts with the correct prefix byte (`0x41` on mainnet) — it does not reject the all-zero address (`0x41 0x00...0x00`), which is structurally a "valid" Tron address but corresponds to no known private key. [1](#0-0) 

### Title
Missing check for the zero address in `AccountPermissionUpdateActuator` allows permanent loss of account ownership - (File: actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java)

### Summary
`AccountPermissionUpdateActuator` lets an account owner update its `Owner`/`Witness`/`Active` permissions by submitting a new set of `Key` addresses via `AccountPermissionUpdateContract`. The only address validation performed on each permission key is `DecodeUtil.addressValid`, which checks length and prefix byte only, never that the address is non-zero.

### Finding Description
In `validate()`, each permission (owner, witness, actives) is checked with `checkPermission()`, which validates each `Key`'s address using `DecodeUtil.addressValid`: [2](#0-1) 

`addressValid` accepts any 21-byte value starting with the network prefix byte, including the "zero address" (prefix byte followed by 20 zero bytes): [1](#0-0) 

In `execute()`, the validated `Owner` permission is written directly into the account via `AccountCapsule.updatePermissions`, replacing the previous owner permission entirely: [3](#0-2) [4](#0-3) 

If an owner mistakenly (or via a client/wallet bug) submits an `Owner` permission whose sole key (or whose weighted keys) is the zero address, the transaction passes validation and is committed. Because no private key exists for the zero address, no future transaction can ever satisfy the new owner permission threshold, permanently locking the account's admin capability — this mirrors the reported `NFTCollection.transferOwnership` bug class of missing `address(0)` checks leading to irrecoverable loss of ownership.

### Impact Explanation
Once the `Owner` permission's active signer set is reduced to only the zero address (or a set whose weighted threshold cannot be met without it), the account can never again execute `AccountPermissionUpdateContract` or any other operation requiring owner-level authorization (e.g. changing permissions back, `WithdrawBalanceActuator`, resource/stake management gated by owner permission, etc.). Any TRX balance or resources tied to that account become permanently inaccessible — a permanent freezing-of-funds / unauthorized loss-of-control condition, consistent with Sherlock's Medium classification for the analogous Solidity issue.

### Likelihood Explanation
This is reachable by any single account holder via one signed `AccountPermissionUpdateContract` transaction — no special privilege, SR/witness status, or multi-party coordination is required. The chance of accidental misuse is non-trivial in any UI/SDK that doesn't itself filter zero addresses, since the chain layer performs no such defense-in-depth check.

### Recommendation
In `AccountPermissionUpdateActuator.checkPermission` (and analogously in `DecodeUtil.addressValid` or a dedicated helper), explicitly reject the all-zero address for permission keys — e.g. add a check that `key.getAddress()` is not equal to the prefix-byte-only zero address before accepting the permission update.

### Proof of Concept
1. Attacker/owner constructs an `AccountPermissionUpdateContract` where `owner.keys[0].address = <prefixByte>+00...00` (21-byte zero address) with weight ≥ threshold, and valid `actives`.
2. Submit the signed transaction; `AccountPermissionUpdateActuator.validate()` passes because `DecodeUtil.addressValid` only checks length/prefix. [5](#0-4) 
3. `execute()` commits the new owner permission via `updatePermissions`, overwriting the previous owner keys.
4. No subsequent transaction can satisfy the owner permission threshold (zero address has no private key), permanently locking owner-level operations on the account.

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

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L47-52)
```java
      byte[] ownerAddress = accountPermissionUpdateContract.getOwnerAddress().toByteArray();
      AccountCapsule account = accountStore.get(ownerAddress);
      account.updatePermissions(accountPermissionUpdateContract.getOwner(),
          accountPermissionUpdateContract.getWitness(),
          accountPermissionUpdateContract.getActivesList());
      accountStore.put(ownerAddress, account);
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

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L178-221)
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
