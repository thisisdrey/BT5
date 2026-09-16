### Title
Missing zero/degenerate-address validation when setting account Owner/Active permission keys permanently locks the account - (File: actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java)

### Summary
`AccountPermissionUpdateActuator` lets an account owner rewrite its own `Owner`, `Witness`, and `Active` permissions via `AccountPermissionUpdateContract`. The only check performed on each permission key's address is `DecodeUtil.addressValid()`, which only checks length (21 bytes) and the `0x41` TRON address prefix, exactly like `transferPosition`'s recipient parameter, which had no restriction beyond being an arbitrary `address`. There is no check that the new `Owner` permission's key corresponds to a spendable/controllable key (e.g., rejecting degenerate all-zero-byte payloads, the black-hole address, or otherwise unrecoverable values). Because `Owner` permission fully controls all subsequent account operations (including the ability to update permissions again), setting it to such an address is unrecoverable — mirroring the "permanently lost" scenario described in the report, but at the level of an entire account's assets/votes/resources rather than a single position.

### Finding Description
`checkPermission()` validates key count, weight, threshold, and permission name length, and for each key only requires `DecodeUtil.addressValid(key.getAddress().toByteArray())`: [1](#0-0) 

`DecodeUtil.addressValid` is a purely syntactic check — length and prefix byte only, with no check that the 20 payload bytes represent a real/derivable key (e.g. it accepts an address consisting of the `0x41` prefix followed by 20 zero bytes, or any other never-controllable value): [2](#0-1) 

The execute step then unconditionally overwrites the account's permissions with whatever passed `validate()`: [3](#0-2) 

Since the `Owner` permission is the root of authority for the account (required for future `AccountPermissionUpdateContract` transactions, for signing/re-delegating `Active`/`Witness` permissions, and for most privileged operations), if the effective `Owner` permission ends up bound to keys nobody can produce a valid signature for (e.g. a copy/paste error, a malicious wallet/dApp front-end that silently substitutes a bogus recipient into the permission-key field, or a fat-fingered value), the account becomes permanently uncontrollable: all TRX/TRC10 balances, frozen/delegated resources, and voting rights tied to that account are frozen forever with no recovery path, exactly the "permanently lost" outcome from the original report, just scoped to a whole account instead of a single bull/bear position.

### Impact Explanation
This results in permanent freezing of funds/resources for any account whose owner mistakenly (or is tricked into) submitting an `AccountPermissionUpdateContract` with a non-recoverable key address for the `Owner` permission. There is no on-chain safeguard against this class of self-inflicted (or phishing-induced) mistake, unlike `TransferContract`/`TransferAssetContract`/`DelegateResourceContract`, which additionally require the receiving account to already exist in the `AccountStore` and reject self-transfers — `AccountPermissionUpdateActuator` has no equivalent recoverability guard on the key material it accepts.

### Likelihood Explanation
`AccountPermissionUpdateContract` is a standard, unprivileged, single-signed transaction any account owner can broadcast; reaching this code path requires no special permissions beyond owning the account being modified. The likelihood of accidental self-lockout is realistic given wallets/dApps compose the `Permission.Key` list programmatically, and a bug or malicious front-end supplying a bad address is analogous to the exact "called by error" scenario described in the original finding.

### Recommendation
Add an explicit rejection of degenerate/unspendable key addresses in `checkPermission()` (e.g., reject the all-zero-payload address and any other reserved/black-hole address) for `Owner`/`Active`/`Witness` permission keys, in addition to the existing `DecodeUtil.addressValid` format check, mirroring the recommendation in the original report to disallow `address(0)`-equivalent values in transfer/ownership-changing functions.

### Proof of Concept
1. Attacker-controlled or buggy client crafts an `AccountPermissionUpdateContract` where the `Owner` permission's single key address is `0x41` followed by 20 zero bytes (passes `DecodeUtil.addressValid`).
2. Account owner signs and broadcasts the transaction (either directly, or via a compromised/buggy wallet integration that fills in this value).
3. `AccountPermissionUpdateActuator.validate()` passes all checks in `checkPermission()` since only format validity is checked.
4. `execute()` calls `account.updatePermissions(...)`, replacing the account's `Owner` permission with the unrecoverable key.
5. No subsequent transaction from this account can ever satisfy the `Owner` permission threshold again (no private key exists for the all-zero payload), permanently freezing the account and all its TRX/TRC10 balances, frozen/delegated resources, and votes.

### Citations

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
