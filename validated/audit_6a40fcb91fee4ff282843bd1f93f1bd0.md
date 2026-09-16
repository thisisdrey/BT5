Confirmed: `DecodeUtil.addressValid` only checks length (21 bytes) and the network prefix byte; it does not reject an all-zero payload (e.g. `0x41` followed by 20 zero bytes) [1](#0-0) . This same helper is the sole address-format check used throughout the actuators, including `AccountPermissionUpdateActuator.checkPermission`, which validates each `Key.getAddress()` in an owner/witness/active `Permission` only via `DecodeUtil.addressValid` [2](#0-1) .

### Title
Account owner permission can be permanently locked to the "zero" address via AccountPermissionUpdateContract - (File: actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java)

### Summary
`AccountPermissionUpdateContract` lets any account holder replace its own Owner/Witness/Active permissions with an arbitrary set of `Key` addresses and weights. The only address-format check applied to each key is `DecodeUtil.addressValid`, which merely checks the byte length and network prefix byte and never checks that the address is non-zero / a real derivable key [3](#0-2) . This mirrors the reported PassThroughWallet bug class: a privileged "owner" role can be set to an unusable/zero address with no dedicated zero-check, permanently freezing management of the entity.

### Finding Description
`checkPermission()` in `AccountPermissionUpdateActuator` validates permission fields (key count, weight sum, threshold, distinctness, operations bitmap) but the address validity check is only `DecodeUtil.addressValid(key.getAddress().toByteArray())` [4](#0-3) . That function accepts any 21-byte array whose first byte equals the chain's address prefix, including one whose remaining 20 bytes are all zero [1](#0-0) . There is no check against the null/blackhole address or against an address with no known/derivable private key. Once `execute()` runs, `account.updatePermissions(...)` overwrites the account's Owner permission (and Active/Witness permissions) with the attacker-chosen (or mistakenly chosen) key set [5](#0-4) .

### Impact Explanation
If an account's Owner permission's sole key (or all keys collectively) is set to an address that nobody controls (e.g., all-zero payload), the account permanently loses the ability to issue any further `AccountPermissionUpdateContract` (or any Active-permission-gated) transactions, since `checkWeight`/`getWeight` in `TransactionCapsule` requires a signature matching one of the permission's keys [6](#0-5) . This is a permanent loss of account control — the account and any TRX/TRC10/resources associated with it (bandwidth/energy delegation, staked balances, votes) become effectively frozen since no one can produce a valid Owner-permission signature to update permissions or authorize privileged operations again.

### Likelihood Explanation
This requires the account owner (or an attacker who can get the owner to sign such a transaction, e.g., via a malicious dApp/tool) to submit a single `AccountPermissionUpdateContract` with a crafted zero/unowned key address. It's self-inflicted or social-engineering-driven rather than exploitable against a third party without their transaction signature, similar to the original report's owner-parameter footgun. Likelihood is moderate: it's a plausible user/tooling error (e.g., a bug in a wallet/SDK default-filling zero bytes) rather than a directly attacker-triggerable theft, but the missing safeguard is a real gap compared to defense-in-depth expected in permission management code.

### Recommendation
Add an explicit check in `AccountPermissionUpdateActuator.checkPermission()` (and anywhere else addresses are accepted as permission keys) to reject the zero address (i.e., prefix byte followed by all-zero bytes) and any other well-known unusable addresses (e.g., the blackhole address), in addition to the existing `DecodeUtil.addressValid` length/prefix check [4](#0-3) .

### Proof of Concept
1. Attacker/owner builds an `AccountPermissionUpdateContract` where `owner.keys[0].address = <prefix_byte><20 zero bytes>` and `weight = threshold`.
2. Submits the transaction signed by the current owner key (valid signature required since this is the account's own permission update).
3. `AccountPermissionUpdateActuator.validate()` passes because `DecodeUtil.addressValid` only checks length/prefix [1](#0-0) .
4. `execute()` persists the new Owner permission with the unusable key [5](#0-4) .
5. No further transaction can satisfy `checkWeight` for the Owner permission, permanently freezing the account's ability to change permissions or perform any Owner-gated action.

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

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L47-52)
```java
      byte[] ownerAddress = accountPermissionUpdateContract.getOwnerAddress().toByteArray();
      AccountCapsule account = accountStore.get(ownerAddress);
      account.updatePermissions(accountPermissionUpdateContract.getOwner(),
          accountPermissionUpdateContract.getWitness(),
          accountPermissionUpdateContract.getActivesList());
      accountStore.put(ownerAddress, account);
```

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L105-117)
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
```

**File:** chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java (L218-226)
```java
  public static long getWeight(Permission permission, byte[] address) {
    List<Key> list = permission.getKeysList();
    for (Key key : list) {
      if (key.getAddress().equals(ByteString.copyFrom(address))) {
        return key.getWeight();
      }
    }
    return 0;
  }
```
