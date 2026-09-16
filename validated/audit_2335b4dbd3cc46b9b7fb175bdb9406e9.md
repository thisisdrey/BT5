### Title
Stale cached signature-verification flag (`isVerified`) is not reset on `rePush`, allowing a transaction signed under revoked/old account permissions to bypass signature re-validation - (File: `framework/src/main/java/org/tron/core/db/Manager.java`)

### Summary
`Manager.rePush()` mirrors the bug class in the `retryMessage()` report: a transaction that was previously validated is re-submitted for execution without re-checking whether the state it depends on (here, the account's signature/permission validity) is still valid at the moment of retry. `rePush` only clears the cached `isVerified` flag on a transaction when the owning address happens to still be present in `ownerAddressSet`; if that address was already removed from the set (e.g., because the permission-changing transaction has already been fully processed), a queued/re-broadcast transaction whose signature was cached as valid under the *old* permission set is pushed straight into `pushTransaction()` and will skip real signature re-validation, exactly as `retryMessage()` skips re-checking `addressBanned`/`proofReceipt` before re-executing a `RETRIABLE` message.

### Finding Description
`Manager.rePush()`: [1](#0-0) 

only calls `tx.setVerified(false)` when `ownerAddressSet.contains(ownerAddress)`. If the owner address is *not* in that set at the time of the retry — which is exactly the race the codebase's own test `getVerifyTxsSkipsBlockWhenPermissionTxAlreadyConsumed` documents (a permission-change tx for owner X is fully consumed and removed from `pendingTransactions`, yet a stale transfer tx signed under the old permission set remains cached with `isVerified=true`) — the cached verification flag is left untouched: [2](#0-1) 

`pushTransaction()` (called by `rePush`) relies on `trx.validateSignature(...)` to gate acceptance: [3](#0-2) 

`TransactionCapsule` caches the result of signature validation in the `isVerified` field so repeated calls short-circuit real cryptographic/permission re-checking (confirmed by the `isVerified`/`setVerified`/`validateSignature` usages in `chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java`). The regression test `testRePushResetsVerifiedOnOwnerAddressSetHit` explicitly demonstrates the intended mitigation only for the "hit" branch: [4](#0-3) 

but there is no equivalent guarantee for the "miss" branch, i.e. when `rePush` is invoked for a transaction whose owner address is no longer tracked in `ownerAddressSet` (normal case for any transaction re-queued from `PendingManager`/`getRePushTransactions` after the original in-flight permission-update batch has completed). In that case the stale `isVerified=true` transaction is pushed and accepted without confirming the signer still satisfies the account's current active/owner permission — the direct analog of `retryMessage()` re-executing a `RETRIABLE` message without re-checking `addressBanned[_addr]`.

### Impact Explanation
If an account owner rotates or revokes a signing key via `AccountPermissionUpdateContract` (or a multi-sig threshold/weight is changed) immediately after broadcasting another transaction signed with the old key, and that older transaction is timed out/requeued through `PendingManager` → `Manager.rePush`, the node can accept and execute it into a block using the old, now-invalid signature validation cache instead of re-verifying against the updated permission set. This allows an unauthorized transaction (signed with a revoked key) to move funds or otherwise act on the account after permissions were intentionally changed to block exactly that key — resulting in unauthorized account operation / theft of funds.

### Likelihood Explanation
Reachable purely through normal user-controlled actions: broadcast a transaction, let it sit in the node's pending/re-push queue (e.g., due to network delay or being popped during a fork), then broadcast an `AccountPermissionUpdateContract` transaction that revokes the key used to sign the first transaction. The race window depends on `PendingManager` timeout and `ownerAddressSet` bookkeeping, both of which are normal node operation paths reachable by any transaction broadcaster, not requiring any privileged or malicious-SR role.

### Recommendation
`Manager.rePush()` should unconditionally call `tx.setVerified(false)` for every transaction being re-pushed (not only when the owner address is present in `ownerAddressSet`), forcing `pushTransaction()`/`validateSignature()` to re-check the transaction's signature against the current account permission state before acceptance — mirroring the report's recommendation to recheck `proofReceipt`/`addressBanned` at the moment of retry rather than relying on state captured earlier.

### Proof of Concept
1. Account X signs Tx1 (e.g., a transfer) with active-permission key K and broadcasts it; the node validates it and caches `isVerified=true`, placing it in `pendingTransactions`.
2. Before Tx1 is included in a block, Tx1 times out out of `pendingTransactions` into `getRePushTransactions` via `PendingManager.close()`/`txIteration`.
3. Account X broadcasts Tx2 (`AccountPermissionUpdateContract`) revoking key K from the active permission; Tx2 is processed and fully committed, after which `ownerAddressSet` no longer contains X's address (per the scenario validated by `getVerifyTxsSkipsBlockWhenPermissionTxAlreadyConsumed`).
4. `Manager.rePush(Tx1)` is invoked from the re-push queue: `ownerAddressSet.contains(ownerAddress)` is false, so `setVerified(false)` is skipped; `pushTransaction(Tx1)` is called with the transaction's stale `isVerified=true` cache still intact.
5. `trx.validateSignature(...)` returns true from the cache without re-checking key K against the now-updated permission set, and Tx1 executes despite K no longer being authorized.

### Citations

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L905-909)
```java
      if (!trx.validateSignature(chainBaseManager.getAccountStore(),
          chainBaseManager.getDynamicPropertiesStore())) {
        throw new ValidateSignatureException(String.format("trans sig validate failed, id: %s",
            trx.getTransactionId()));
      }
```

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L2134-2147)
```java
  public void rePush(TransactionCapsule tx) {
    if (containsTransaction(tx)) {
      return;
    }

    String ownerAddress = ByteArray.toHexString(tx.getOwnerAddress());
    synchronized (this) {
      if (ownerAddressSet.contains(ownerAddress)) {
        tx.setVerified(false);
      }
    }

    try {
      this.pushTransaction(tx);
```

**File:** framework/src/test/java/org/tron/core/db/ManagerTest.java (L890-923)
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
```

**File:** framework/src/test/java/org/tron/core/db/ManagerTest.java (L1606-1633)
```java
  @Test
  public void testRePushResetsVerifiedOnOwnerAddressSetHit() throws Exception {
    TransferContract transferContract = TransferContract.newBuilder()
        .setAmount(1L)
        .setOwnerAddress(ByteString.copyFrom(
            ByteArray.fromHexString(Wallet.getAddressPreFixString()
                + "548794500882809695A8A687866E76D4271A1ABC")))
        .setToAddress(ByteString.copyFrom(
            ByteArray.fromHexString(Wallet.getAddressPreFixString()
                + "A389132D6639FBDA4FBC8B659264E6B7C90DB086")))
        .build();
    TransactionCapsule tx = new TransactionCapsule(transferContract, ContractType.TransferContract);
    tx.setVerified(true); // simulate mempool-cached state

    String ownerAddress = ByteArray.toHexString(tx.getOwnerAddress());

    // Inject ownerAddress into ownerAddressSet via reflection
    Set<String> ownerAddressSet =
        (Set<String>) ReflectUtils.getFieldObject(dbManager, "ownerAddressSet");
    ownerAddressSet.add(ownerAddress);

    // rePush should reset isVerified to false before pushTransaction
    dbManager.rePush(tx);

    // After rePush, isVerified must be false
    Boolean verified = (Boolean) ReflectUtils.getFieldObject(tx, "isVerified");
    Assert.assertFalse(verified);
  }
```
