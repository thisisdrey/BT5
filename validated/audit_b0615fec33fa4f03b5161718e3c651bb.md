## Title
Missing zero-address check in `DecodeUtil.addressValid` allows account permissions (and other critical addresses) to be irreversibly set to the all-zero address, permanently freezing funds - (File: `common/src/main/java/org/tron/common/utils/DecodeUtil.java`)

### Summary
`DecodeUtil.addressValid()` — the canonical address-validation routine used throughout java-tron's actuators — only checks that an address is non-empty, exactly 21 bytes long, and starts with the correct network prefix byte (`0x41`). It never rejects the "zero address" (prefix byte followed by 20 zero bytes). Every actuator that relies solely on this helper to sanity-check a critical address therefore accepts the zero address as "valid." This is the exact bug class described in the external report (`AccountantDelegate.initialize()` missing a zero-address check for `treasury_`): a format/type check exists, but the semantically dangerous "zero value" is never excluded.

### Finding Description
`DecodeUtil.addressValid` is defined as: [1](#0-0) 

It validates only length and prefix byte — never whether the remaining 20 bytes are all zero.

`AccountPermissionUpdateActuator.checkPermission()`, invoked from `validate()` for a standard, unprivileged `AccountPermissionUpdateContract` transaction, uses exactly this helper as the sole check on each permission `Key`'s address: [2](#0-1) 

Because `addressValid` accepts the all-zero address, a transaction signer can set an `Owner`/`Active`/`Witness` permission's key(s) to the zero address (with sufficient weight to satisfy the threshold). Once `execute()` persists this permission update, the corresponding permission is controlled by an address for which no one possesses a private key. Any subsequent transaction (including `TransferContract`, `TransferAssetContract`, `FreezeBalanceV2Contract`, etc.) requiring that permission's signature can never again be authorized, permanently freezing whatever balance/assets the affected permission slot(s) protect (e.g. the `Owner` permission, which typically has full control).

The same missing check is repeated across many other actuators that use `DecodeUtil.addressValid` as their only address sanity check (e.g. `TransferActuator`, `TransferAssetActuator`, `DelegateResourceActuator`, `FreezeBalanceActuator`/`FreezeBalanceV2Actuator` receiver/owner addresses), meaning the same root cause (`DecodeUtil.addressValid`) underlies multiple places where a critical address parameter can be silently set to the unusable zero address instead of being rejected.

### Impact Explanation
When the `Owner` permission of an account is updated to require the zero address's signature (directly, or via a distinct-address list containing the zero address with enough weight to meet the threshold), the account's ability to authorize future transactions with that permission is permanently and irrecoverably lost. This is a "permanent freezing of funds" scenario: TRX, TRC-10 assets, frozen/staked balances, and any resource delegated from that account become inaccessible, with no path to recovery — mirroring the judge's rationale in the original report ("risk of loss of funds, and the inability to easily fix").

### Likelihood Explanation
Any account holder can independently broadcast an `AccountPermissionUpdateContract` transaction (requires `AllowMultiSign` to be enabled, which is already active on mainnet) that includes a permission key equal to the zero address; `checkPermission` will not reject it. This can happen accidentally (e.g. a wallet/tool bug producing an unset/zeroed address field) or be leveraged in phishing/social-engineering flows that trick a victim into signing a permission update containing a bogus zero-value key. No special privilege beyond normal transaction signing is required.

### Recommendation
Harden `DecodeUtil.addressValid` (or add an explicit additional check at each critical call site, especially `AccountPermissionUpdateActuator.checkPermission`) to reject addresses whose payload bytes (bytes 1-20) are all zero, in addition to the existing length/prefix checks. At minimum, add an explicit zero-address check for permission `Key` addresses in `AccountPermissionUpdateActuator` before persisting the new permissions.

### Proof of Concept
1. Attacker/careless user crafts an `AccountPermissionUpdateContract` for account `A`, setting the `Owner` permission's single key to `Key{ address = 0x41 || 20×0x00, weight = 1 }` with `threshold = 1`.
2. `AccountPermissionUpdateActuator.validate()` calls `checkPermission(owner)`, which calls `DecodeUtil.addressValid(zeroAddressBytes)` — this returns `true` because length is 21 and the prefix byte matches; the all-zero payload is never checked. [3](#0-2) 
3. `execute()` persists the new `Owner` permission on account `A`.
4. From that point on, no valid private key can produce a signature satisfying the `Owner` permission, so any transaction requiring it (transfers, freezes, etc.) will always fail signature verification, permanently locking the account's funds.

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
