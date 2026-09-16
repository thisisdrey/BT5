## Finding

### Title
Single-step account permission update allows irreversible loss of Owner control - (File: `actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java`)

### Summary
`AccountPermissionUpdateActuator` lets an account's current Owner permission holder overwrite the account's `owner`, `witness`, and `active` permissions in a single atomic step, with no two-step "propose then accept" flow analogous to `Ownable2Step`. If the new Owner permission's keys are mistyped or point to addresses the caller does not control, the account's owner permission is replaced immediately and irreversibly, exactly the "wrong address passed during ownership transfer" scenario the reported issue warns about.

### Finding Description
`execute()` unconditionally calls `account.updatePermissions(...)` with the new `owner`, `witness`, and `actives` permissions taken directly from the `AccountPermissionUpdateContract`, replacing the existing permission set in one step: [1](#0-0) 

`validate()` and the helper `checkPermission()` only check structural well-formedness of the new permission (address format validity via `DecodeUtil.addressValid`, key count bounds, and that `weightSum >= threshold`) — they never verify that the new keys are actually controlled by anyone reachable, or require any confirmation/acceptance step from the new key holders: [2](#0-1) 

Because the update takes effect for `PermissionType.Owner` immediately upon a single valid transaction (as opposed to a two-step `propose`/`accept` pattern like `Ownable2Step`), any typo in a key address, or specification of an address whose private key is unknown/lost, permanently and irrecoverably replaces the account's owner authority. There is no mechanism to reverse the change since, post-update, control is defined solely by the new (possibly unreachable) permission set.

### Impact Explanation
If the new `owner` `Permission` keys are set to an address not controlled by the account holder (typo, wrong copy-paste, or loss of the intended key), the account permanently loses the ability to authorize any further `AccountPermissionUpdateContract`, transfers requiring Owner-level authorization, or other owner-gated operations. This results in permanent freezing of the account's TRX/TRC10/resources under that account, matching the "permanent freezing of funds" impact criterion. This applies to any account (including exchanges/custodians) that uses multisig permission updates via this actuator.

### Likelihood Explanation
Likelihood is low-to-moderate: it requires an operational mistake by the account owner (wrong address entry) rather than an attacker forcing it, mirroring the original report's own likelihood characterization ("low, because it requires an error on the admin side"). However, `AccountPermissionUpdateContract` is a standard, frequently used multisig-management transaction type reachable by any account holder once `AllowMultiSign` is enabled, so the exposure surface is broad.

### Recommendation
Introduce a two-step permission transfer flow analogous to `Ownable2Step`: require the new Owner permission update to be "proposed" (stored pending) and then separately "accepted"/confirmed by a transaction signed under the new permission's keys before it takes effect, rather than committing the new `owner` permission unconditionally in a single `AccountPermissionUpdateContract` execution.

### Proof of Concept
1. Account `A` has `AllowMultiSign` enabled and a normal Owner permission with keys it controls.
2. `A` submits an `AccountPermissionUpdateContract` (routed to `AccountPermissionUpdateActuator`) setting the new `owner` `Permission.keys` to an address `X` that `A` does not control (e.g., due to a typo, or an intended cold-wallet address whose key was never generated/is lost).
3. `validate()` passes because `X` is a structurally valid address and `weightSum >= threshold`: [3](#0-2) 
4. `execute()` commits the update, overwriting the account's owner permission: [1](#0-0) 
5. Account `A` can no longer produce a valid Owner-permission signature (since no one controls `X`), permanently freezing any Owner-gated operations on the account.

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
