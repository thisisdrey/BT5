## Analysis

The reported bug class is: a privileged "owner"-style field can be overwritten with no sanity/zero-check and no reversible/2-step process, permanently locking every feature gated behind that owner check.

The closest reachable analog in java-tron is the `AccountPermissionUpdateContract` flow, which lets any account holder replace their `OwnerPermission` (and `ActivePermission`/`WitnessPermission`) in a single signed transaction, processed by `AccountPermissionUpdateActuator`.

### Root cause

`AccountPermissionUpdateActuator.checkPermission()` only validates internal consistency of the submitted permission (key count bounds, distinct addresses, valid address format, positive weights, `weightSum >= threshold`, operations bitmap for Active permissions). It never checks that the *new* owner permission is actually reachable/controllable going forward — e.g. it does not require inclusion of the transaction's own signing key, nor any other safeguard against setting the new owner-permission keys to addresses whose private keys the submitter does not control: [1](#0-0) 

`validate()` similarly only checks that `ownerAddress` is a valid, existing account and that the contract structurally has an owner/active permission — again with no protection against the new owner set being unreachable by the account's controller: [2](#0-1) 

`execute()` then unconditionally overwrites the account's permissions with whatever was submitted, with no rollback, staging, or 2-step "pending owner" confirmation mechanism: [3](#0-2) 

Because `AccountPermissionUpdateContract` itself can only be re-executed by satisfying the *current* owner-permission threshold (enforced elsewhere via multi-sig signature-weight validation against the stored `OwnerPermission`), once the owner-permission keys are set to an unreachable/faulty set of addresses, there is no path to ever change permissions, vote, freeze/unfreeze TRX, delegate resources, or perform any owner/active-permission-gated action on that account again — mirroring exactly the impact described in the report ("access to all these features will be lost and cannot be recovered").

### Title
Uncontrolled overwrite of account OwnerPermission in AccountPermissionUpdateActuator can permanently lock an account - (File: actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java)

### Summary
`AccountPermissionUpdateActuator` allows the current owner of a TRON account to replace the account's `OwnerPermission`, `WitnessPermission`, and `ActivePermission` sets in a single signed transaction. Neither `validate()` nor `checkPermission()` verifies that the new owner-permission key set remains reachable/controllable by the account owner (e.g., that it still contains an address the submitter controls, or matches keys previously proven controllable). The update is applied immediately and irreversibly with no staged/2-step confirmation, matching the reported bug class of "uncontrolled update of ownership."

### Finding Description
`checkPermission()` (lines 71-146) only enforces syntactic/structural constraints: key count bounds, distinct addresses, valid `DecodeUtil.addressValid` addresses, positive key weights, `weightSum >= threshold`, and the 32-byte operations bitmap for Active permissions. None of these checks require that the resulting `OwnerPermission` remain controllable by anyone who can currently sign for the account, nor that the submitting key be retained in the new key set. `validate()` (lines 148-229) likewise only checks that `ownerAddress` is valid and the account exists — it performs no "does this new configuration still allow future control" check analogous to a zero-address check. `execute()` (lines 34-69) then commits the new permissions directly via `AccountCapsule.updatePermissions` and persists it to `AccountStore`, with no pending/staged owner concept, unlike a safe 2-step ownership transfer.

### Impact Explanation
If a user (or a malicious dApp/wallet that convinces a user to sign a crafted `AccountPermissionUpdateContract`) sets the new `OwnerPermission` keys to addresses the account holder does not actually control (analogous to setting `owner` to a wrong/uncontrolled address in the report), the account permanently loses the ability to satisfy its own owner-permission signature-weight threshold. Since every subsequent `AccountPermissionUpdateContract` (and other owner/active-permission gated operations) requires satisfying that same threshold to execute, the account becomes irrecoverably locked — freezing the account holder's TRX, resource delegation, voting rights, and multi-sig management capability forever, with no recovery path (no timelock, no committee override, no 2-step handoff).

### Likelihood Explanation
This requires only a single, unprivileged, correctly-signed `AccountPermissionUpdateContract` transaction from the account's current owner — exactly the kind of "single signed transaction" input this scan is scoped to. No special network position, SR/witness privilege, or additional exploit chain is required; a phishing dApp or a buggy wallet UI generating this transaction is sufficient to trigger permanent loss of control, mirroring the original report's "faulty address during initialize/transferOwnership" scenario.

### Recommendation
Add a safeguard in `AccountPermissionUpdateActuator.validate()`/`checkPermission()` requiring that the new `OwnerPermission` remains satisfiable by the party executing the update (e.g., require the submitting/signing address, or a previously-verified controllable key, to be present in the new owner key set with nonzero weight), and/or introduce a staged/2-step confirmation flow (propose new permission, then confirm from a key already in the new set) analogous to `pendingOwner`/`claimOwnership` patterns, rather than allowing an unconditional, single-transaction overwrite of `OwnerPermission`.

### Proof of Concept
1. Account `A` currently has `OwnerPermission` = `{key: A, weight: 1, threshold: 1}` (the default set at account creation, see `AccountCapsule.createDefaultOwnerPermission`).
2. `A` signs and broadcasts an `AccountPermissionUpdateContract` with `owner_address = A` and a new `owner` permission whose only key is address `B` (an address `A` does not control), with `threshold = 1`, `weight = 1` — this passes all checks in `checkPermission()` (valid address, positive weight, weightSum(1) >= threshold(1)) and `validate()` (structurally complete, account exists).
3. `AccountPermissionUpdateActuator.execute()` commits this new `OwnerPermission` to `AccountCapsule` via `updatePermissions` and persists it.
4. From this point on, any transaction requiring owner-permission signature weight for account `A` (including a subsequent `AccountPermissionUpdateContract` attempting to fix the mistake) can only be authorized by a signature from `B`. Since `A` never controlled `B`'s private key, account `A`'s owner-gated functionality (permission changes, and any operation requiring `OwnerPermission`) is permanently and irrecoverably locked.

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

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L71-122)
```java
  private boolean checkPermission(Permission permission) throws ContractValidateException {
    DynamicPropertiesStore dynamicStore = chainBaseManager.getDynamicPropertiesStore();
    if (permission.getKeysCount() > dynamicStore.getTotalSignNum()) {
      throw new ContractValidateException("number of keys in permission should not be greater "
          + "than " + dynamicStore.getTotalSignNum());
    }
    if (permission.getKeysCount() == 0) {
      throw new ContractValidateException("key's count should be greater than 0");
    }
    if (permission.getType() == PermissionType.Witness && permission.getKeysCount() != 1) {
      throw new ContractValidateException("Witness permission's key count should be 1");
    }
    if (permission.getThreshold() <= 0) {
      throw new ContractValidateException("permission's threshold should be greater than 0");
    }
    String name = permission.getPermissionName();
    if (!StringUtils.isEmpty(name) && name.length() > 32) {
      throw new ContractValidateException("permission's name is too long");
    }
    //check owner name ?
    if (permission.getParentId() != 0) {
      throw new ContractValidateException("permission's parent should be owner");
    }

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
