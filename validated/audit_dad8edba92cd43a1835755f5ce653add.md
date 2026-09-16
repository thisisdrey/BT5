Confirmed: `AccountPermissionUpdateActuator.validate()` (and the transaction-level `checkPermission` in `TransactionCapsule.java:635-645`) never checks that the *new* permission structure being written is bounded by the operations/authority of the *signing* permission. This is the exact structural analog of the OpenClaw bug: an "approval"/update action is authorized only by checking that the caller holds *some* permission whose operations bitmap includes `AccountPermissionUpdateContract`, but the code never subsets the newly granted scope against the caller's own scope before writing it.

### Title
Privilege escalation via unbounded Owner/Active permission rewrite in AccountPermissionUpdateContract - (File: actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java)

### Summary
`AccountPermissionUpdateActuator` lets any account holder authorize an `AccountPermissionUpdateContract` using a non-Owner "Active" permission, as long as that Active permission's operation bitmap allows the `AccountPermissionUpdateContract` type. The actuator's `validate()` only checks the *structural* well-formedness of the new `owner`/`witness`/`active` permissions being set (key counts, weights, thresholds, valid addresses, and that the requested `active.operations` bitmap is a subset of the chain-wide *available* contract types) — it never checks that the new permission's granted scope (weight/threshold/operations) is bounded by the scope of the permission that authorized the transaction. Once `execute()` runs, `AccountCapsule.updatePermissions()` unconditionally overwrites the account's Owner permission and all Active permissions with the attacker-supplied values.

### Finding Description
- The signature/permission check that authorizes the transaction happens in `TransactionCapsule.validateSignature()` / `checkPermission(int permissionId, Permission permission, Transaction.Contract contract)` at [1](#0-0)  and [2](#0-1) . This check only requires: (a) `permissionId != 0` → permission type must be `Active`, and (b) the Active permission's `operations` bitmap must have the bit set for `AccountPermissionUpdateContract`. It says nothing about what the transaction's *payload* is allowed to set.
- The actuator's `validate()` at [3](#0-2)  only validates the new `owner`/`witness`/`actives` permissions for internal consistency (`checkPermission(Permission permission)` at lines 71-146) — key count ≤ `TotalSignNum`, threshold > 0, weight sum ≥ threshold, and (for Active only) that `operations` is a subset of `dynamicStore.getAvailableContractType()` (i.e., all chain-supported contract types), **not** a subset of the signer's own permission's `operations`.
- `execute()` then calls `AccountCapsule.updatePermissions(owner, witness, actives)` at [4](#0-3) , which unconditionally replaces the account's Owner permission (`builder.setOwnerPermission(owner)`) and clears/rebuilds all Active permissions, with no scope comparison against the permission used to authorize the transaction.
- Consequence: an account owner who has issued a narrow-scope Active permission key (e.g., a bot/operational key whose `operations` bitmap only enables `AccountPermissionUpdateContract`, intended only for routine key rotation within that same narrow scope) can use that single low-privilege key to rewrite the account's Owner permission to itself (single key, threshold 1) and grant a new Active permission with a full `operations` bitmap covering every contract type (`TransferContract`, `WitnessCreateContract`, `ExchangeContract`, `TriggerSmartContract`, etc.). This mirrors the OpenClaw flaw exactly: the "approval" (permission update) action never subsets the requested/granted authority against the caller's own held authority.

### Impact Explanation
Any account that issues a multi-sig "Active" key scoped only to allow `AccountPermissionUpdateContract` (a common minimal-privilege pattern for automated key-rotation bots) grants that key holder the ability to fully take over the account — reassigning Owner permission and all Active permissions to itself — completely bypassing the intended weight/threshold separation between Owner and Active tiers. This is unauthorized account takeover / privilege escalation, directly enabling theft of funds (subsequent `TransferContract`, `WithdrawBalanceContract`, `TransferAssetContract`, etc., signed with the newly self-granted Owner key) and permanent loss of the original owner's control.

### Likelihood Explanation
Any account using TRON's multisig feature and delegating an Active-tier key with `AccountPermissionUpdateContract` in its operations bitmap (a documented, common way to allow non-owner delegated key rotation) is directly exposed. No special chain state, timing, or witness/SR privileges are required — a single signed transaction from the attacker-held Active key is sufficient, reachable via ordinary transaction broadcast.

### Recommendation
In `AccountPermissionUpdateActuator.validate()`, when the authorizing `permissionId` (from the transaction contract, via `chainBaseManager`) is not the Owner permission (id 0), require that every key/weight/threshold and `operations` bit in the proposed `owner`, `witness`, and `active` permissions be a subset of (no broader than) the operations/weight already granted by the signing Active permission. Alternatively, restrict `AccountPermissionUpdateContract` to require Owner-permission (`permissionId == 0`) signatures only, removing the ability for non-owner Active keys to modify the permission structure at all.

### Proof of Concept
1. Owner account `A` sets up permissions via `AccountPermissionUpdateContract`: Owner permission = key `A` weight 1 threshold 1; Active permission (id=2) = key `B` weight 1 threshold 1, `operations` bitmap with only the bit for `AccountPermissionUpdateContract` set (all other bits 0) — a narrow "rotate-my-own-active-key" bot key.
2. Attacker controlling private key `B` crafts a new `AccountPermissionUpdateContract` transaction with `permission_id = 2` (referencing the narrow Active permission), setting: `owner` = {key `B`, weight 1, threshold 1} and `actives[0]` = {key `B`, weight 1, threshold 1, `operations` = all-bits-set (full authority)}.
3. Sign the transaction with only key `B`. `TransactionCapsule.checkPermission()` at [1](#0-0)  passes because the Active permission's operations bit for `AccountPermissionUpdateContract` is set; `checkWeight()` passes because `B`'s weight (1) meets the Active permission's threshold (1).
4. `AccountPermissionUpdateActuator.validate()` passes because the new `owner`/`active` permissions are structurally valid (non-zero threshold/weights, valid addresses, `operations` subset of chain-available contract types).
5. `execute()` → `AccountCapsule.updatePermissions()` overwrites Owner permission to `{B}` and Active permission to `{B, full operations}` — `B` now fully controls account `A`, despite originally only holding a narrowly-scoped Active key.

### Citations

**File:** chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java (L468-496)
```java
  public static boolean validateSignature(Transaction transaction,
      byte[] hash, AccountStore accountStore, DynamicPropertiesStore dynamicPropertiesStore)
      throws PermissionException, SignatureException, SignatureFormatException {
    Transaction.Contract contract = transaction.getRawData().getContractList().get(0);
    int permissionId = contract.getPermissionId();
    byte[] owner = getOwner(contract);
    AccountCapsule account = accountStore.get(owner);
    Permission permission = null;
    if (account == null) {
      if (permissionId == 0) {
        permission = AccountCapsule.getDefaultPermission(ByteString.copyFrom(owner));
      }
      if (permissionId == 2) {
        permission = AccountCapsule
            .createDefaultActivePermission(ByteString.copyFrom(owner), dynamicPropertiesStore);
      }
    } else {
      permission = account.getPermissionById(permissionId);
    }
    if (permission == null) {
      throw new PermissionException("permission isn't exit");
    }
    checkPermission(permissionId, permission, contract);
    long weight = checkWeight(permission, transaction.getSignatureList(), hash, null);
    if (weight >= permission.getThreshold()) {
      return true;
    }
    return false;
  }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java (L635-645)
```java
  private static void checkPermission(int permissionId, Permission permission, Transaction.Contract contract) throws PermissionException {
    if (permissionId != 0) {
      if (permission.getType() != PermissionType.Active) {
        throw new PermissionException("Permission type is error");
      }
      //check operations
      if (!checkPermissionOperations(permission, contract)) {
        throw new PermissionException("Permission denied");
      }
    }
  }
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
