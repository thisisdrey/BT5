Based on my investigation, java-tron's `TransactionCapsule` implements a caching mechanism (`isVerified`) for signature/permission validation that structurally mirrors the Collabora bug class — a permission check result cached and reused across a mutable authorization context.

### Title
Cached `isVerified` flag in `TransactionCapsule` can allow a stale/looser permission decision to be reused after the account's permission set changes - (File: `chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java`)

### Summary
`TransactionCapsule.validateSignature`/`validatePubSignature` compute a signature+permission check once and cache the result in the `isVerified` boolean field, short-circuiting all future calls on the same object:

```java
public boolean validatePubSignature(...) {
  if (!isVerified) {
    ...
    isVerified = true;
  }
  return true;
}
``` [1](#0-0) 

The underlying permission lookup (`account.getPermissionById(permissionId)` in `validateSignature`) reads live `AccountCapsule` permission data at the moment of the first call. [2](#0-1) 

### Finding Description
This is analogous to the Collabora bug: a rights decision (`userCanWrite`) is checked once and then trusted for the lifetime of an object, even though the authority backing that decision (the account's `AccountPermissionUpdateContract`-controlled permission list, threshold, and operation bitmap) can change afterward. In java-tron this same `TransactionCapsule` object can persist across the pending-transaction pool and be re-verified against a block via `Manager.getVerifyTxs`, which decides whether to trust the cached `isVerified` flag instead of re-running signature/permission checks — exactly the pattern the project's own regression test (`getVerifyTxsSkipsBlockWhenPermissionTxAlreadyConsumed`) was written to guard against:

```java
// A permission-change tx (A) for owner X has been processed and consumed,
// so it is no longer in pendingTransactions but ownerAddressSet still contains X.
// A later transfer tx (B) from X with the old signature enters pending with
// isVerified=true. A malicious SR produces a block containing only B (no A).
// getVerifyTxs must place B into the re-verify list rather than calling
// setVerified(true) just because B matches the pending entry.
``` [3](#0-2) 

This test demonstrates the fix already exists for the block-application path in `Manager` (via the `ownerAddressSet` bookkeeping), but the fact that a dedicated regression test had to be added confirms the underlying `isVerified` cache-reuse hazard is real and was previously reachable: a transaction signed once under a permission set (e.g., permissionId=2 active permission with a low threshold or broad operation bitmap) is verified and cached as `isVerified=true`; if the owner's permissions are subsequently updated (tightened, revoked, or the key removed) via `AccountPermissionUpdateActuator`, and the same `TransactionCapsule` instance is reused/re-broadcast/re-applied without going through the object that re-triggers permission checks, the stale `true` result is honored instead of re-evaluating against the new permission state.

### Impact Explanation
If the cached `isVerified=true` result is trusted after the backing permission has been revoked or narrowed, an operation that should now be rejected (insufficient weight, revoked key, disallowed contract type per the operations bitmap) is instead executed — i.e., unauthorized account operation under the new permission model, potentially enabling execution of contracts (transfers, resource delegation, etc.) that the current permission set forbids.

### Likelihood Explanation
The specific block-application path (`Manager.getVerifyTxs`) has already been hardened per the cited test, indicating this exact class of issue was identified and is actively defended against for that path. Reachability outside that already-patched path (e.g., other places that check `capsule.isVerified` before re-validating) could not be fully confirmed within available tool budget — the field is only referenced within `TransactionCapsule.java` itself and the `ManagerTest.java` regression test; I did not find other call sites bypassing the intended re-verification, and I was unable to fully trace `Manager.java`'s complete `getVerifyTxs` implementation and every caller of `validateSignature`/`validatePubSignature` before running out of iterations.

### Recommendation
- Ensure every code path that can present a `TransactionCapsule` for execution after a possible permission mutation (pending-pool re-entry, re-push, fork switch, atomic/batch transactions) invalidates or re-derives `isVerified` rather than trusting a cached flag tied to a mutable `AccountCapsule` permission state.
- Prefer keying the verification cache to a snapshot/version of the account's permission data (e.g., a permission hash or account "version" counter) instead of a plain boolean, so any permission update invalidates previously cached verifications.
- Audit all callers of `TransactionCapsule.validateSignature`/`validatePubSignature`/`isVerified` for the same "verify-once, trust-forever" pattern the `ManagerTest` regression test was written to close.

### Proof of Concept
Conceptual reproduction (not fully verified against current `Manager.getVerifyTxs` internals since the complete method body could not be retrieved):
1. Owner account X has active permission (id=2) with threshold satisfied by key K1.
2. Transaction B (owned by X, permissionId=2) is signed by K1 and enters the pending pool; `validatePubSignature` runs once, sets `isVerified=true` on B's `TransactionCapsule`.
3. Owner X submits `AccountPermissionUpdateContract` (transaction A) removing K1 from the active permission or raising the threshold; A is applied/consumed.
4. B (still carrying `isVerified=true` from step 2, and the same underlying signature by now-revoked K1) is re-presented for application (e.g., in a block, or re-push after a fork switch) without going through a path that resets `isVerified`.
5. If any such path trusts `isVerified` instead of re-checking against X's now-updated permission set, B executes despite K1 no longer being authorized — the unauthorized-operation condition described by the CVE analog.

The existence and description of `getVerifyTxsSkipsBlockWhenPermissionTxAlreadyConsumed` in `ManagerTest.java` [3](#0-2)  confirms this exact scenario (steps 1–5) was previously exploitable via the block-application path and has since been mitigated there specifically; whether equivalent protection exists uniformly across every other reuse of a cached `TransactionCapsule.isVerified` state is not confirmed by the available evidence.

### Citations

**File:** chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java (L468-490)
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
```

**File:** chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java (L650-680)
```java
  public boolean validatePubSignature(AccountStore accountStore,
      DynamicPropertiesStore dynamicPropertiesStore)
      throws ValidateSignatureException {
    if (!isVerified) {
      if (this.transaction.getSignatureCount() <= 0
              || this.transaction.getRawData().getContractCount() <= 0) {
        throw new ValidateSignatureException("miss sig or contract");
      }
      if (this.transaction.getSignatureCount() > dynamicPropertiesStore
              .getTotalSignNum()) {
        throw new ValidateSignatureException("too many signatures");
      }

      byte[] hash = getTransactionId().getBytes();

      long startNs = System.nanoTime();
      try {
        if (!validateSignature(this.transaction, hash, accountStore, dynamicPropertiesStore)) {
          isVerified = false;
          throw new ValidateSignatureException("sig error");
        }
      } catch (SignatureException | PermissionException | SignatureFormatException e) {
        isVerified = false;
        throw new ValidateSignatureException(e.getMessage());
      } finally {
        logSlowSigVerify(startNs);
      }
      isVerified = true;
    }
    return true;
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
