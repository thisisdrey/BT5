Confirmed: `DecodeUtil.addressValid()` only checks length (21 bytes) and the network prefix byte (`0x41`) [1](#0-0) . It does not reject an all-zero body (e.g. `0x41` followed by 20 zero bytes), which is a syntactically valid but unowned/unusable address, analogous to the "zero address" concept in the report.

### Title
Owner Permission of `AccountPermissionUpdateContract` Can Be Set to Unrecoverable (Zero-Body) Addresses, Permanently Locking Account Control - (File: actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java)

### Summary
`AccountPermissionUpdateActuator.checkPermission()` validates that key addresses in a permission are well-formed via `DecodeUtil.addressValid()`, but that check only verifies address length and prefix byte, not that the address corresponds to a spendable/controllable key (e.g., non-zero body) [2](#0-1) .

### Finding Description
The `AccountPermissionUpdateContract` lets any account owner reassign their `Owner`/`Witness`/`Active` permissions to a new set of keys, thresholds, and weights, executed directly by `AccountPermissionUpdateActuator.execute()` which calls `AccountCapsule.updatePermissions()` and persists the new permission set with no further checks [3](#0-2) . The only address-level validation in `checkPermission()` is `DecodeUtil.addressValid()`, which checks that the address is 21 bytes long and starts with the correct prefix byte — it does not verify the address body is non-zero or otherwise corresponds to a key an account can actually control [4](#0-3) [1](#0-0) . If an owner mistakenly (or due to a buggy wallet/dApp) sets the `Owner` permission's keys to such an unusable address (analogous to the zero-minter bug in the report), the account permanently loses the ability to sign future `AccountPermissionUpdateContract` transactions to fix the mistake, since no valid private key exists for that address.

### Impact Explanation
This results in permanent loss of account control: the account can never again change its owner/active/witness permissions, effectively freezing any funds or witness/voting rights tied to that account's multi-sig configuration — matching the "permanent freezing of funds"/unauthorized loss-of-control class of impact.

### Likelihood Explanation
This requires the account owner's own signature to trigger (self-inflicted, similar to the original report where an authorized caller passes a bad argument), so it is not attacker-forced against a victim without some social-engineering or wallet-bug vector, but it is directly reachable by any unprivileged transaction broadcaster with no protocol-level protection against it, and the described report itself treats such a self-inflicted config mistake as a valid finding.

### Recommendation
In `AccountPermissionUpdateActuator.checkPermission()`, in addition to `DecodeUtil.addressValid()`, explicitly reject keys whose address body is all-zero (or otherwise known-unspendable/black-hole addresses), and consider requiring at least one key in the new `Owner` permission to remain verifiable/controllable (e.g., disallow replacing all owner keys with unverifiable addresses in a single update without safeguards).

### Proof of Concept
1. Attacker/owner submits an `AccountPermissionUpdateContract` transaction signed with their current owner key, setting the new `Owner` permission's `Key.address` to `0x41` followed by 20 zero bytes, with `threshold = weight` of that single key.
2. `AccountPermissionUpdateActuator.validate()` calls `checkPermission(owner)`, which only checks `DecodeUtil.addressValid()` (length/prefix) — this passes since the crafted address is syntactically valid [5](#0-4) .
3. `execute()` persists the new owner permission via `account.updatePermissions(...)` [6](#0-5) .
4. No private key exists for the zero-body address, so no future `AccountPermissionUpdateContract` (or any owner-permission-gated action) can ever be validly signed for that account again — permanent loss of control.

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

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L41-52)
```java
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
