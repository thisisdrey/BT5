## Analysis

The Snipe-IT bug is a broken-access-control (CWE-863) case where a user with only a narrow permission-editing capability can use a legitimate self-service update endpoint to grant themselves arbitrary additional permissions, because the endpoint never checks that the new grants are bounded by what the caller was actually authorized to assign.

java-tron has a structurally identical gap in its multi-sig permission system: `AccountPermissionUpdateContract` lets an account rewrite its own `owner`/`witness`/`active` permissions in a single transaction, and it can be authorized to run with only a *narrowly scoped* `Active` permission (one whose 32-byte `operations` bitmap only has the `AccountPermissionUpdateContract` bit set, bit 46). The actuator that executes this contract never checks that the *new* permission set it is about to install is a subset of the operations the signer was originally delegated — it only checks that the requested operations correspond to *some* valid contract type on the chain.

### Root cause [1](#0-0) 
`checkPermission()` validates a submitted `Permission`'s `operations` bitmap only against `dynamicStore.getAvailableContractType()` (i.e. "is this a real contract type"), never against the operations bitmap of the permission that is actually signing/authorizing the transaction. [2](#0-1) 
`execute()` then unconditionally overwrites `owner`, `witness`, and all `active` permissions on the account with whatever was submitted via `AccountCapsule.updatePermissions`, with no re-check against the caller's original authorized scope. [3](#0-2) 
`updatePermissions` blindly replaces the owner permission, witness permission (id=1), and the entire `active_permission` list (ids 2..9) with attacker-supplied values.

The only gate that restricts *which* transaction types a given `Active` permission may sign is `WalletUtil.checkPermissionOperations`, which checks a single bit (`contract.getTypeValue()`) for the contract type being executed *right now* — it has no notion of "this permission may only ever grant permissions that are itself a subset of its own operations bitmap": [4](#0-3) 

This means a delegated `Active` key that was intentionally scoped to *only* manage multisig configuration (bit 46, `AccountPermissionUpdateContract`, set — and nothing else, e.g. no `TransferContract`, `WithdrawBalanceContract`, `FreezeBalanceContract`, etc.) can use that single narrow grant to rewrite the account's `Owner` permission and all `Active` permissions to give itself (or any address it controls) full, unrestricted control of the account — including funds transfer, asset issuance, voting, freezing/unfreezing, and future permission changes.

### Title
Self Privilege Escalation via AccountPermissionUpdateContract: a delegate with only permission-management authority can grant itself full account control - (File: actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java)

### Summary
An `Active` permission key that is delegated *solely* the right to call `AccountPermissionUpdateContract` (operations bit 46) can use that authority to unilaterally rewrite the account's `Owner` permission and all `Active` permissions, granting itself unrestricted authority over every other contract type on the account, including asset transfers and balance withdrawals.

### Finding Description
`AccountPermissionUpdateActuator.checkPermission()` only validates that a submitted permission's `operations` bitmap corresponds to valid, currently-available `ContractType`s [5](#0-4) . It never compares the requested new operations set to the operations bitmap of the permission that authorized/signed the current transaction. `execute()` then commits the new `owner`/`witness`/`actives` wholesale via `AccountCapsule.updatePermissions` [6](#0-5) [3](#0-2) . Because the `Owner` permission itself carries no `operations` restriction (checked and enforced empty for non-Active types) [7](#0-6) , replacing it grants the new key-holder unrestricted control over the account for every contract type, not just permission management.

### Impact Explanation
An account owner who delegates a narrowly-scoped `Active` key (e.g., an "ops/compliance" key permitted only to adjust multisig configuration) is exposed to full account takeover by that delegate: transfer of TRX/TRC10/TRC20 balances, asset issuance, voting, freeze/unfreeze, and withdrawal — a concrete unauthorized-account-operation / theft-of-funds impact matching the "Accept only concrete unauthorized account operation, theft ... of funds" bar.

### Likelihood Explanation
This requires the account owner to have configured a tiered multisig setup delegating `AccountPermissionUpdateContract` authority narrowly to a non-owner `Active` key — a realistic and commonly recommended custody pattern (e.g., an "admin" key that manages signer rotation without holding funds-moving rights). Any single signed `AccountPermissionUpdateContract` transaction from that delegated key is sufficient; no additional cooperation from other signers or the account owner is needed.

### Recommendation
When processing `AccountPermissionUpdateContract`, the actuator should determine the `Permission` that actually authorized the current transaction (via `contract.getPermissionId()`, mirroring the logic in `TransactionCapsule.validateSignature`) and reject any new `owner`/`witness`/`active` permission whose requested `operations` bitmap grants bits that are not already a subset of the authorizing permission's own `operations` (unless the authorizing permission is the account's `Owner` permission, id 0). This bounds delegated permission-management authority so it cannot be used to bootstrap broader account control.

### Proof of Concept
1. Account `A` sets up multisig: `Owner` permission (threshold requiring `A`'s own key), and an `Active` permission `P2` assigned to delegate key `D` with `operations` bitmap having **only** bit 46 (`AccountPermissionUpdateContract`) set — `D` cannot sign `TransferContract`, `WithdrawBalanceContract`, `FreezeBalanceContract`, etc.
2. `D` signs an `AccountPermissionUpdateContract` transaction with `permission_id = 2` (referencing `P2`), `owner_address = A`.
3. In the payload, `D` sets a new `Owner` permission with threshold 1 and key `D` (or sets a new `Active` permission for `D` with an `operations` bitmap containing all bits, e.g. transfer/withdraw/freeze/vote types).
4. `WalletUtil.checkPermissionOperations` passes because bit 46 is set on `P2`, the signing permission. `AccountPermissionUpdateActuator.checkPermission()` passes because every bit in the new permission's `operations` corresponds to a real `ContractType` in `dynamicStore.getAvailableContractType()` — there is no check that the new bits are limited to what `P2` was scoped to.
5. `execute()` commits the new `Owner`/`Active` permissions. `D` now has full unrestricted control of account `A`, including transferring all its funds.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L41-59)
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

      adjustBalance(accountStore, ownerAddress, -fee);
      if (chainBaseManager.getDynamicPropertiesStore().supportBlackHoleOptimization()) {
        chainBaseManager.getDynamicPropertiesStore().burnTrx(fee);
      } else {
        adjustBalance(accountStore, accountStore.getBlackhole(), fee);
      }
```

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L124-145)
```java
    ByteString operations = permission.getOperations();
    if (permission.getType() != PermissionType.Active) {
      if (!operations.isEmpty()) {
        throw new ContractValidateException(
            permission.getType() + " permission needn't operations");
      }
      return true;
    }
    //check operations
    if (operations.isEmpty() || operations.size() != 32) {
      throw new ContractValidateException("operations size must 32");
    }

    byte[] types1 = dynamicStore.getAvailableContractType();
    for (int i = 0; i < 256; i++) {
      boolean b = (operations.byteAt(i / 8) & (1 << (i % 8))) != 0;
      boolean t = ((types1[(i / 8)] & 0xff) & (1 << (i % 8))) != 0;
      if (b && !t) {
        throw new ContractValidateException(i + " isn't a validate ContractType");
      }
    }
    return true;
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

**File:** chainbase/src/main/java/org/tron/common/utils/WalletUtil.java (L27-37)
```java
  public static boolean checkPermissionOperations(Permission permission, Contract contract)
      throws PermissionException {
    ByteString operations = permission.getOperations();
    if (operations.size() != 32) {
      throw new PermissionException(String.format("operations size must 32, actual: %d",
          operations.size()));
    }
    int contractType = contract.getTypeValue();
    boolean b = (operations.byteAt(contractType / 8) & (1 << (contractType % 8))) != 0;
    return b;
  }
```
