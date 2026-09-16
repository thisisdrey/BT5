Confirmed: `DecodeUtil.addressValid` (`common/src/main/java/org/tron/common/utils/DecodeUtil.java:15-33`) only validates address length (21 bytes) and the network prefix byte — it does not, and cannot, verify that the address corresponds to a key someone actually controls, is not the all-zero/burn address, or is an "active" account. This is the direct analog of the `Ownable.sol` `transferOwnership` check (`_newOwner != address(0)`) which the report flags as insufficient. [1](#0-0) 

`AccountPermissionUpdateActuator.validate()` uses exactly this weak check on every key address in the new `owner`/`witness`/`active` permissions, and `execute()` overwrites the account's permissions unconditionally once validation passes. [2](#0-1) [3](#0-2) 

### Title
Insufficient new-owner address validation in AccountPermissionUpdateActuator permits permanent account lockout - (File: actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java)

### Summary
`AccountPermissionUpdateContract` lets an account owner replace their `Owner`, `Witness`, and `Active` permissions with an arbitrary set of key addresses. The only address check performed, `DecodeUtil.addressValid`, verifies address length and network prefix byte only — it never confirms that the address is not the zero/black-hole address or that any party actually holds its private key. [3](#0-2) [1](#0-0) 

### Finding Description
`checkPermission()` in `AccountPermissionUpdateActuator` iterates every `Key` in a permission and only rejects the key if `DecodeUtil.addressValid()` returns false (wrong length or wrong prefix byte), plus enforces weight/threshold arithmetic and distinctness — but never checks that the key address is non-zero or otherwise reachable/controlled. [4](#0-3) 

`validate()` similarly checks the transaction's `ownerAddress` only for format validity, not for the new permission keys' controllability, before `execute()` unconditionally overwrites `AccountCapsule` permissions via `account.updatePermissions(...)`. [5](#0-4) [6](#0-5) 

Because any 21-byte, correctly-prefixed byte sequence passes `addressValid`, a mistake or typo in the submitted `AccountPermissionUpdateContract` (e.g., a bit-flipped or truncated/re-derived address, or the canonical black-hole address) results in a syntactically valid but uncontrolled address being installed as the sole/majority-weight `Owner` key. This exactly mirrors the `Ownable.sol` weakness cited in the report, where only the zero-address is excluded and no "is this address actually usable" check exists.

### Impact Explanation
Once the `Owner` permission's keys are set to addresses nobody controls (and the weighted threshold can no longer be met by any real signer), the account is permanently locked out of any operation gated by the `Owner` permission (e.g., future `AccountPermissionUpdateContract` transactions to fix the mistake, and other owner-gated operations), permanently freezing the account and any funds/resources tied to it. This matches the "permanent freezing of funds" impact category.

### Likelihood Explanation
Likelihood is low: the actor triggering this is the account owner itself, acting on its own signed transaction; there is no way for a third party to force this outcome. Just as the original report notes, this is a self-inflicted misconfiguration risk rather than an externally exploitable attack, and the affected blast radius is limited to the single account performing the update rather than "the whole protocol."

### Recommendation
Add an explicit safeguard in `AccountPermissionUpdateActuator.checkPermission()`/`validate()` rejecting the reserved black-hole/zero address as a permission key, and consider requiring at least one key in the new `Owner` permission to remain under demonstrable control (e.g., matching the transaction's own signer set) before allowing the update to be committed, so a single erroneous update cannot brick owner-level permissions irrecoverably.

### Proof of Concept
1. Attacker (or careless owner) crafts an `AccountPermissionUpdateContract` for `ownerAddress` with a new `Owner` permission whose sole `Key` is a valid-format (21-byte, correct prefix) address that is provably uncontrolled (e.g., `410000000000000000000000000000000000000000`, the network's black-hole address) with `weight == threshold`.
2. Submit the signed transaction; `AccountPermissionUpdateActuator.validate()` passes because `DecodeUtil.addressValid` only checks length/prefix (`common/src/main/java/org/tron/common/utils/DecodeUtil.java:15-33`), and `checkPermission()` accepts the key since weightSum ≥ threshold.
3. `execute()` calls `account.updatePermissions(...)` and persists the new `Owner` permission (`actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java:49-52`).
4. The account can no longer produce a valid `Owner`-permission signature for any subsequent `AccountPermissionUpdateContract`, permanently freezing owner-gated functionality for that account.

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

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L105-122)
```java
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

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L178-181)
```java
    byte[] ownerAddress = accountPermissionUpdateContract.getOwnerAddress().toByteArray();
    if (!DecodeUtil.addressValid(ownerAddress)) {
      throw new ContractValidateException("invalidate ownerAddress");
    }
```
