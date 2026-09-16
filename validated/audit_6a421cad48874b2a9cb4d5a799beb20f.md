Based on the investigation, this is the mechanism relevant to the CVE analogy: `Manager.getVerifyTxs` uses an `ownerAddressSet` to decide whether a pending transaction's cached `isVerified` flag can be trusted when a block references it, specifically to guard against a transfer transaction that was signed under an *old* permission/key set being replayed after that account's permission was changed (analogous to "old sessions still valid after 2FA is enabled").

### Title
Stale cached signature verification (`isVerified`) can accept a transaction signed under a superseded account permission - (File: `framework/src/main/java/org/tron/core/db/Manager.java`)

### Summary
`Manager` maintains a pending-transaction pool where each `TransactionCapsule` carries an `isVerified` flag set the first time its signature/permission check succeeds. When a block is applied, `getVerifyTxs` decides whether to trust that cached flag or force a fresh signature/permission re-check. The dedicated regression test `getVerifyTxsSkipsBlockWhenPermissionTxAlreadyConsumed` in `ManagerTest.java` documents exactly the failure mode this guards against: a permission-change transaction for an owner address is consumed and removed from `pendingTransactions`, but a second transaction from the same owner — pre-verified under the *old* permission set — could otherwise be blindly accepted as `isVerified=true` purely because it matches a pending-pool entry, even though the owner's permission set backing that verification is stale.

### Finding Description
`AccountPermissionUpdateActuator.execute()` replaces an account's `owner`/`witness`/`active` permissions in place via `AccountCapsule.updatePermissions()` [1](#0-0) , and `AccountCapsule.updatePermissions` overwrites the stored `Permission` protobuf fields directly [2](#0-1) . Signature validity for any transaction is computed against whatever permission is currently stored for the owner via `TransactionCapsule.validateSignature`, which fetches `account.getPermissionById(permissionId)` and checks the signer weights against that live permission object [3](#0-2) . Because verification result is cached as a boolean `isVerified` on the `TransactionCapsule` sitting in the mempool/pending pool, and blocks are later matched against that pool by content equality (transaction id / signature set) rather than by re-deriving permission state, there is a narrow window where a transaction verified under the pre-update permission set could be accepted into a block without being forced through re-verification against the new permission set, effectively letting an old (superseded) authorization path remain "logged in" after the account's permission/keys were rotated — the direct analog of Cal.com's stale-session-after-2FA issue. The `ownerAddressSet` bookkeeping in `Manager` [4](#0-3)  exists specifically to force re-verification in this owner-had-a-permission-change scenario.

### Impact Explanation
If the re-verification safeguard fails or is bypassed on some code path (e.g., pool eviction ordering, or an alternate ingestion path that doesn't consult `ownerAddressSet`), a malicious block producer could include a transaction whose signature was only valid under an account's *old* permission set (e.g., a compromised key that the owner just rotated out via `AccountPermissionUpdateContract`) and have it accepted as already-verified, allowing unauthorized account operations (asset transfer, contract call) to execute after the legitimate owner believed the old key was revoked.

### Likelihood Explanation
This requires: (1) an account owner submitting an `AccountPermissionUpdateContract` to rotate keys/weights, (2) an attacker with a signature valid under the pre-rotation permission racing to get a transaction into the pending pool before/around the same time, and (3) a block producer (potentially malicious/careless) assembling a block that includes only the attacker's transaction, not the permission-update transaction, in the exact ordering that would let cached `isVerified=true` be trusted without a full permission re-check. The existing test coverage (`getVerifyTxsTest`, `getVerifyTxsSkipsBlockWhenPermissionTxAlreadyConsumed`) indicates the maintainers specifically hardened `getVerifyTxs` against this scenario, meaning the primary path is currently covered — but the bug-class (trusting a cached verification flag across a permission-changing event) is exactly the CVE's analog and worth targeted regression review on any code paths that bypass `getVerifyTxs`/`ownerAddressSet` (e.g. direct `pushTransaction`/mempool re-add paths).

### Recommendation
Ensure every code path that can add a `TransactionCapsule` to `pendingTransactions` or mark it `isVerified=true` is required to pass through the `ownerAddressSet` staleness check in `Manager.getVerifyTxs` (or an equivalent check) whenever the owner address has had a permission-altering contract (`AccountPermissionUpdateContract`) processed after the transaction was originally verified. Add explicit invalidation of pooled/cached verification state for an owner address immediately upon successful execution of `AccountPermissionUpdateActuator`, rather than relying solely on best-effort set tracking in `Manager`.

### Proof of Concept
Not independently reproducible from static review alone — the existing test `getVerifyTxsSkipsBlockWhenPermissionTxAlreadyConsumed` [4](#0-3)  demonstrates the exact scenario and asserts the current code handles it correctly (forces `bTx` into the re-verify list). A concrete exploit PoC would require confirming a real path that adds a transaction to the pending pool as `isVerified=true` *without* going through `getVerifyTxs`'s `ownerAddressSet` check after a permission update — this was not found within the indexed portion of `Manager.java` (the file's `getVerifyTxs`/pool-management implementation body was not available in the index beyond the test file's description). A Devin session with full repository access would be needed to trace `Manager.java`'s complete `pushTransaction`, `pendingTransactions` mutation, and `ownerAddressSet` update logic line-by-line to confirm whether any bypass currently exists.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L43-52)
```java
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

**File:** framework/src/test/java/org/tron/core/db/ManagerTest.java (L890-929)
```java
  @Test
  public void getVerifyTxsSkipsBlockWhenPermissionTxAlreadyConsumed() throws Exception {
    // Scenario: a permission-change tx (A) for owner X has been processed and consumed,
    // so it is no longer in pendingTransactions but ownerAddressSet still contains X.
    // A later transfer tx (B) from X with the old signature enters pending with
    // isVerified=true. A malicious SR produces a block containing only B (no A).
    // getVerifyTxs must place B into the re-verify list rather than calling
    // setVerified(true) just because B matches the pending entry.
    TransferContract bContract = TransferContract.newBuilder()
        .setOwnerAddress(ByteString.copyFrom("f1".getBytes()))
        .setAmount(7).build();
    TransactionCapsule bTx = new TransactionCapsule(bContract, ContractType.TransferContract);
    String hexOwner = ByteArray.toHexString("f1".getBytes());

    dbManager.getPendingTransactions().clear();
    dbManager.getPendingTransactions().add(bTx);

    Field field = Manager.class.getDeclaredField("ownerAddressSet");
    field.setAccessible(true);
    @SuppressWarnings("unchecked")
    Set<String> ownerAddressSet = (Set<String>) field.get(dbManager);
    Set<String> backup = new HashSet<>(ownerAddressSet);
    ownerAddressSet.clear();
    ownerAddressSet.add(hexOwner);

    try {
      List<Transaction> blockTxs = new ArrayList<>();
      blockTxs.add(bTx.getInstance());
      BlockCapsule capsule = new BlockCapsule(0, ByteString.EMPTY, 0, blockTxs);

      List<TransactionCapsule> txs = dbManager.getVerifyTxs(capsule);

      Assert.assertEquals(1, txs.size());
      Assert.assertEquals(bTx.getTransactionId(), txs.get(0).getTransactionId());
    } finally {
      ownerAddressSet.clear();
      ownerAddressSet.addAll(backup);
      dbManager.getPendingTransactions().clear();
    }
  }
```
