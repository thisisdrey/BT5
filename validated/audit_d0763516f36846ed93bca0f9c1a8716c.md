Based on my research, the closest reachable analog to the LSP6 "residual universal permission" bug is in java-tron's own multisig permission subsystem (`AccountPermissionUpdateContract` / `AccountCapsule` permission model), not in the TVM CREATE2 storage lifecycle — CREATE2 storage is already namespaced per-deployment-tx via `Storage.generateAddrHash(trxId)` [1](#0-0) , so no residual-storage analog exists there.

### Title
Witness Permission (permission_id=1) Cannot Be Revoked and Remains Signature-Valid After an Account Stops Being a Witness - (File: chainbase/src/main/java/org/tron/core/capsule/AccountCapsule.java)

### Summary
`AccountCapsule.updatePermissions()` only overwrites the account's witness permission slot (`id=1`) when the caller supplies a non-null `witness` Permission **and** the account is currently a witness; it never clears the existing witness permission otherwise [2](#0-1) . Meanwhile `getPermissionById(1)` returns whatever witness permission is stored, without checking whether the account is presently a witness [3](#0-2) .

### Finding Description
When an account becomes a witness, an `AccountPermissionUpdateContract` sets `witness_permission` (id=1) with a specific key set/threshold [4](#0-3) . `AccountPermissionUpdateActuator.validate()` forbids setting a witness permission unless the account is currently marked as a witness (`accountCapsule.getIsWitness()`), and forbids omitting one if it is: `"account isn't witness can't set witness permission"` [5](#0-4) .

This means once an account loses witness status (or was never fully synced), the owner has **no supported way to submit a permission update that clears the stale `witness_permission`** — any attempt to include a witness field is rejected by validate(), and any attempt without one leaves the old field untouched, because `updatePermissions()`'s witness branch is gated on `builder.getIsWitness()` at execute time [6](#0-5) . The stale permission record (old keys/threshold set when the account was previously entrusted with witness authority) survives in the `AccountCapsule` state indefinitely.

Independently, both transaction-signature validation paths — `TransactionCapsule.validateSignature()` (used when a transaction is broadcast) and `TransactionCapsule.addSign()` (used when co-signers add signatures to a multisig transaction) — resolve the effective permission purely via `account.getPermissionById(permissionId)`, with no re-check of `account.getIsWitness()` [7](#0-6) [8](#0-7) . Any signed transaction that sets `permission_id = 1` in its `Transaction.Contract` will be authorized against the old, now-orphaned witness permission if it exists — this is analogous to LSP6's universal, owner-independent permission keys that are never invalidated when the entity's status/ownership changes.

### Impact Explanation
An address that was previously granted witness-level signing authority (e.g., an operations key, a former co-signer, or a key that the current controller believed was revoked) retains latent, unrevocable control over any operation gated by permission id=1 even after the account is no longer a witness and even though the current controller has no on-chain mechanism to clear it. If the account becomes a witness again later (witness status is a separate flag, not itself gated by `AccountPermissionUpdateContract`), the old permission automatically becomes "live" again without any new sanctioned permission-update transaction. This can enable unauthorized account operations by a stale/former key holder.

### Likelihood Explanation
This requires (1) an account to have set a witness permission at some point and (2) later become/be non-witness or reconfigure permissions, and (3) an attacker to hold or have retained one of the previously-authorized witness keys. This is a plausible but conditional scenario, similar to the C4-Medium rating given to the original LSP6 finding, which also required "unfounded trust" assumptions about revocation. It is reachable purely through normal, unprivileged transaction broadcast/signing — no special SR/witness/node privilege is needed to exploit stale key retention, only to have previously held such a key.

### Recommendation
- In `AccountCapsule.updatePermissions()`, always clear `witness_permission` when either the incoming `witness` argument is null or the account is not currently a witness, mirroring the unconditional `clearActivePermission()` behavior already used for active permissions.
- Alternatively/additionally, in `TransactionCapsule.validateSignature()` / `addSign()`, when `permissionId == 1`, explicitly verify `account.getIsWitness()` is true before honoring the stored witness permission, so a stale permission entry cannot be used to authorize transactions once witness status is lost.

### Proof of Concept
1. Account `A` becomes a witness and sets `witness_permission` (id=1) to keys `{K1}` via `AccountPermissionUpdateContract` [9](#0-8) .
2. `A` later stops being a witness (`getIsWitness()` becomes false) through the normal witness-resignation/committee flow.
3. `A`'s current controller submits a new `AccountPermissionUpdateContract` to reorganize owner/active permissions; `validate()` rejects any attempt to include a witness field (`"account isn't witness can't set witness permission"`), so the stale `witness_permission{K1}` cannot be cleared [10](#0-9) ; `updatePermissions()` executes without touching the witness slot [6](#0-5) .
4. Anyone holding key `K1` crafts and signs a transaction from account `A` with `permission_id = 1`. `validateSignature()`/`addSign()` fetch `account.getPermissionById(1)`, which still returns `{K1}` regardless of `A`'s current witness status, and the transaction is authorized [11](#0-10) .

Note: I was unable to fully inspect the body of the `checkPermission(int permissionId, Permission permission, Transaction.Contract contract)` helper (which validates the permission's `operations` bitmap against the contract type) before running out of iterations, so I cannot confirm the exact set of `ContractType`s reachable via a stale witness permission — this would need to be verified in a full session to determine the precise operations achievable (e.g., `WithdrawBalanceContract`, `VoteWitnessContract`, or a broader default bitmap).

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/program/Storage.java (L61-71)
```java
  private static byte[] addrHash(byte[] address, byte[] trxHash) {
    if (ByteUtil.isNullOrZeroArray(trxHash)) {
      return Hash.sha3(address);
    }
    return Hash.sha3(ByteUtil.merge(address, trxHash));
  }

  public void generateAddrHash(byte[] trxId) {
    // update addreHash for create2
    addrHash = addrHash(address, trxId);
  }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/AccountCapsule.java (L1280-1299)
```java
  public Permission getPermissionById(int id) {
    if (id == 0) {
      if (this.account.hasOwnerPermission()) {
        return this.account.getOwnerPermission();
      }
      return getDefaultPermission(this.account.getAddress());
    }
    if (id == 1) {
      if (this.account.hasWitnessPermission()) {
        return this.account.getWitnessPermission();
      }
      return null;
    }
    for (Permission permission : this.account.getActivePermissionList()) {
      if (id == permission.getId()) {
        return permission;
      }
    }
    return null;
  }
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

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L34-52)
```java
  @Override
  public boolean execute(Object object) throws ContractExeException {
    TransactionResultCapsule result = (TransactionResultCapsule) object;
    if (Objects.isNull(result)) {
      throw new RuntimeException(ActuatorConstant.TX_RESULT_NULL);
    }

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

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L191-199)
```java
    if (accountCapsule.getIsWitness()) {
      if (!accountPermissionUpdateContract.hasWitness()) {
        throw new ContractValidateException("witness permission is missed");
      }
    } else {
      if (accountPermissionUpdateContract.hasWitness()) {
        throw new ContractValidateException("account isn't witness can't set witness permission");
      }
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L208-221)
```java
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
```

**File:** chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java (L468-495)
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
```

**File:** chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java (L597-610)
```java
  public void addSign(byte[] privateKey, AccountStore accountStore)
      throws PermissionException, SignatureException, SignatureFormatException {
    Transaction.Contract contract = this.transaction.getRawData().getContract(0);
    int permissionId = contract.getPermissionId();
    byte[] owner = getOwnerAddress();
    AccountCapsule account = accountStore.get(owner);
    if (account == null) {
      throw new PermissionException("Account is not exist!");
    }
    Permission permission = account.getPermissionById(permissionId);
    if (permission == null) {
      throw new PermissionException("permission isn't exit");
    }
    checkPermission(permissionId, permission, contract);
```
