## Analysis

The Sherlock finding describes a class of bug: a privileged actor submits a transaction containing a **full-state overwrite** (a "fullRow" bitmap) that was computed off-chain from a stale read of on-chain state. Because there is no freshness/version check, a legitimate update submitted by someone else in the interim gets silently clobbered when the stale full-state write lands.

The closest reachable analog in java-tron is `AccountPermissionUpdateActuator`, which lets an account's multi-sig owner group replace the **entire** permission bundle (owner/witness/active permissions) in one shot, with no compare-and-swap against the permission state the transaction was built against. [1](#0-0) 

The overwrite itself happens in `AccountCapsule.updatePermissions()`, which unconditionally sets the owner permission, sets the witness permission if present, and `clearActivePermission()`s before rebuilding the full active-permission list from the caller-supplied data: [2](#0-1) 

`validate()` only checks structural correctness of the supplied permissions (key counts, thresholds, weights, contract-type bitmap) — it never compares the submitted permission set against the account's *current* permission state to detect staleness: [3](#0-2) 

### Why this matches the bug class
A TRON account can define an Owner permission with multiple keys and a threshold below the total key count (e.g. 3 keys, threshold 2). Two disjoint subsets of key-holders satisfying that threshold can each independently build and sign a *different* `AccountPermissionUpdateContract` (e.g., group {K1,K2} adds key K4 to Active permissions; group {K1,K3} adds key K5), both computed against the same pre-existing on-chain permission snapshot. Both transactions are individually valid and can be broadcast. Whichever lands later in a block silently overwrites the other's change via the unconditional `clearActivePermission()` + rebuild, because there is no nonce/version tied to the permission object being updated. The result is the same "rollback of a legitimately-made, quorum-approved update" pattern the Sherlock report flags — except here the asset at risk is account custody (signer sets), not grant applications.

### Title
Unversioned full-permission overwrite in `AccountPermissionUpdateActuator` allows one valid multisig update to silently roll back another - (File: `actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java`)

### Summary
`AccountPermissionUpdateActuator.execute()` calls `AccountCapsule.updatePermissions()`, which performs a blind full-row replacement of Owner/Witness/Active permissions with no check that the submitted permission set is based on the account's current on-chain state. Two independently-signed, individually-valid `AccountPermissionUpdateContract` transactions built from different threshold-satisfying key subsets of the same Owner permission can race; the later-mined one wholesale overwrites the earlier one's changes, silently discarding a legitimately authorized permission update (e.g., a newly added Active key), analogous to the `reviewRecipients()`/`statusesBitMap` full-row overwrite issue.

### Finding Description
`validate()` performs only structural checks (key uniqueness, weight sums, threshold, contract-type bitmap) and never compares the incoming permission structure against the account's existing permission state or any freshness token/nonce. `execute()` then unconditionally overwrites the owner permission, optionally the witness permission, and clears+rebuilds the entire active permission list from the transaction payload: [2](#0-1) 
Because the Owner permission model supports N keys with threshold T < N, disjoint quorum-satisfying subsets can each craft a valid, differently-intentioned `AccountPermissionUpdateContract`. Neither transaction has any way to detect that the other is "in flight" or has already landed, since there's no compare-and-swap on the stored `Permission` objects.

### Impact Explanation
Whichever transaction is included later completely replaces the permission set, discarding the other quorum's legitimately authorized change (e.g., reinstating a removed key, or dropping a newly added key/threshold change) without any error or revert. This is a state-integrity/unauthorized-account-operation issue for shared-custody accounts: a signer group can have its authorized permission update silently reverted by another concurrently-submitted, equally valid update, potentially restoring access for a key that was intentionally being removed, or removing access that was intentionally being granted.

### Likelihood Explanation
Requires an account configured with a multi-key Owner permission whose threshold is satisfiable by more than one disjoint key subset (a legitimate and expected multisig configuration, not a misconfiguration), and two of those subsets independently building conflicting permission updates around the same time — plausible in real shared-custody/DAO-style wallets where changes are proposed by different sub-groups without off-chain coordination.

### Recommendation
Tie `AccountPermissionUpdateContract` to an expected prior-state reference (e.g., include a hash or version/sequence number of the currently-active permission set, incremented on every successful update) and have `validate()`/`execute()` reject the update if the account's current permission state no longer matches what the transaction was built against, forcing the submitter to re-derive and re-sign against the latest state.

### Proof of Concept
1. Configure account `A` with Owner permission: keys `K1, K2, K3`, threshold `2`.
2. Group `{K1,K2}` builds `AccountPermissionUpdateContract` #1 (based on current state) adding new Active key `K4`, signs, but delays broadcast.
3. Group `{K1,K3}` builds `AccountPermissionUpdateContract` #2 (based on same current state) adding new Active key `K5`, signs, and broadcasts first — it is mined, `updatePermissions()` sets Active list to include `K5`.
4. Group `{K1,K2}` then broadcasts contract #1 (still referencing the pre-#2 state). It passes `validate()` (no freshness check) and `execute()` overwrites the Active permission list wholesale with the set from step 2, silently discarding `K5` and restoring the list to include only `K4` — group `{K1,K3}`'s authorized change is lost with no error emitted to either party.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L44-52)
```java
    try {
      accountPermissionUpdateContract = any.unpack(AccountPermissionUpdateContract.class);

      byte[] ownerAddress = accountPermissionUpdateContract.getOwnerAddress().toByteArray();
      AccountCapsule account = accountStore.get(ownerAddress);
      account.updatePermissions(accountPermissionUpdateContract.getOwner(),
          accountPermissionUpdateContract.getWitness(),
          accountPermissionUpdateContract.getActivesList());
      accountStore.put(ownerAddress, account);
```

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L148-228)
```java
  @Override
  public boolean validate() throws ContractValidateException {

    if (chainBaseManager == null) {
      throw new ContractValidateException(ActuatorConstant.STORE_NOT_EXIST);
    }

    if (this.any == null) {
      throw new ContractValidateException(ActuatorConstant.CONTRACT_NOT_EXIST);
    }

    AccountStore accountStore = chainBaseManager.getAccountStore();
    DynamicPropertiesStore dynamicStore = chainBaseManager.getDynamicPropertiesStore();

    if (dynamicStore.getAllowMultiSign() != 1) {
      throw new ContractValidateException("multi sign is not allowed, "
          + "need to be opened by the committee");
    }
    if (!this.any.is(AccountPermissionUpdateContract.class)) {
      throw new ContractValidateException(
          "contract type error,expected type [AccountPermissionUpdateContract],real type["
              + any.getClass() + "]");
    }
    final AccountPermissionUpdateContract accountPermissionUpdateContract;
    try {
      accountPermissionUpdateContract = any.unpack(AccountPermissionUpdateContract.class);
    } catch (InvalidProtocolBufferException e) {
      logger.debug(e.getMessage(), e);
      throw new ContractValidateException(e.getMessage());
    }
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
