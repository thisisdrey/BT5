### Title
Stale signature-verification cache lets a revoked/changed permission key still authorize a transaction applied in the same block - ([File: framework/src/main/java/org/tron/core/db/Manager.java])

### Summary
`Manager.getVerifyTxs()` marks a block's transactions as already `verified` whenever an identical-signature copy exists in the node's local `pendingTransactions` pool, letting `processTransaction()`/`TransactionCapsule.validatePubSignature()` skip re-checking the signature against the *current* on-chain permission state. This mirrors the Open WebUI flaw: one code path (fresh HTTP-style validation) consults up-to-date authorization state while another path (a cached/pre-validated decision) does not re-check it after the authorization state changes.

### Finding Description
`TransactionCapsule.validatePubSignature()` short-circuits entirely when `isVerified == true`: [1](#0-0) 

`isVerified` is set to `true` not only after a genuine fresh signature check, but also opportunistically by `Manager.getVerifyTxs()`, which compares a block's transactions against the node's `pendingTransactions` pool purely by signature-byte equality, with no re-check of the signer's current permission weight or the account's permission version: [2](#0-1) 

The resulting `txs` list (with some entries pre-flagged `verified`) is passed into `applyBlock`/`processTransaction`, which will skip `validatePubSignature`'s permission/weight check for those entries: [3](#0-2) [4](#0-3) 

The permission/weight check that would have been skipped is `TransactionCapsule.validateSignature()` → `checkPermission()`/`checkWeight()`, which reads the *account's current* `Permission` from `AccountStore` at validation time: [5](#0-4) 

Notably, the codebase already recognizes and has patched exactly this class of bug for the fork-switch path — `switchFork()` explicitly clears `isVerified` before reapplying transactions because "the new branch is applied on a rewound, diverged state where account permissions may have changed, so a cached signature-verification result is no longer trustworthy": [6](#0-5) 

However, this exact same class of staleness is not addressed for the mainline `pushBlock` → `getVerifyTxs` → `applyBlock` path when a transaction is found in `pendingTransactions` with an identical signature. If an `AccountPermissionUpdateContract` transaction (or any transaction changing the owner/active permission — e.g., removing a key, lowering its weight, or raising the threshold) for account X is applied earlier in the same block (or in an immediately preceding block already reflected in `AccountStore`) than a pooled transaction A from account X signed by key K, `getVerifyTxs` will still mark A `verified=true` based on A's presence (with the same signature bytes) in the pre-existing local mempool — a state that predates the permission change. `validatePubSignature` then trusts that stale flag and never re-derives A's signer weight against the *post-update* `Permission`.

### Impact Explanation
This allows a transaction signed by a key that has just been removed from (or had its weight/threshold changed in) an account's permission to still execute successfully as long as it was sitting in the local mempool with an identical signature before the permission-changing transaction landed. This is a concrete "unauthorized account operation" primitive: an account owner (or an attacker who briefly had signing rights) can race a permission-revocation transaction against a pre-broadcast, already-pooled transaction signed with the soon-to-be-revoked key, and have the node still apply the revoked-key transaction in the very block that revokes it — bypassing the multisig/permission authorization model that `AccountPermissionUpdateActuator` and `checkWeight` are supposed to enforce network-wide. Because block application must be deterministic across all nodes, this discrepancy (nodes with vs. without the tx in their pool reaching different verification outcomes) can also cause validation/consensus disagreement between nodes.

### Likelihood Explanation
Requires the transaction to already exist, with byte-identical signature, in the specific node's `pendingTransactions` pool at the time the block is pushed — this is the normal, expected state for the block's own producing/witness node and for any node that received the transaction via normal p2p relay before the block, which is the typical case for legitimately broadcast transactions. No special privilege beyond normal transaction broadcasting and an account with permission-management authority (over its own account, or coordinated with a cooperating signer) is needed, matching the requirement that the analog be reachable by unprivileged transaction broadcasters.

### Recommendation
Apply the same fix already used for `switchFork()` to the mainline path: do not let `getVerifyTxs()`'s pool-membership match alone set `isVerified = true`. Either (a) also re-validate the signer's current permission weight before marking `verified`, or (b) invalidate/clear `isVerified` for any transaction whose owner address is touched by a permission-changing contract type earlier in block ordering, analogous to how `is_token_revoked` is now consulted uniformly across all authentication entry points in the Open WebUI fix.

### Proof of Concept
1. Account X has active permission with signer key K (weight sufficient to meet threshold).
2. Broadcast transaction A (e.g., TransferContract) from X signed only by K; let it sit in the target node's `pendingTransactions` pool (validated successfully under the current permission).
3. Broadcast/produce a block containing, in order: (i) an `AccountPermissionUpdateContract` transaction from X that removes K from the active permission (or drops its weight below threshold), followed by (ii) transaction A with the identical signature bytes already in the pool.
4. On `pushBlock`, `getVerifyTxs()` matches A's signature against the pooled copy and sets `capsule.setVerified(true)` before `applyBlock` executes the block's transactions in order.
5. During `processTransaction` for A, `validatePubSignature()` sees `isVerified == true` and returns immediately without re-deriving K's weight against the just-updated permission — A executes despite K no longer being authorized under X's current permission.

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

**File:** chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java (L650-661)
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
```

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L1143-1156)
```java
        try (ISession tmpSession = revokingStore.buildSession()) {
          if (!item.getBlk().validateSignature(
              getDynamicPropertiesStore(), getAccountStore())) {
            throw new ValidateSignatureException(
                "switch fork: block " + item.getBlk().getNum() + " signature invalid");
          }
          // The new branch is applied on a rewound, diverged state where account permissions
          // may have changed, so a cached signature-verification result is no longer
          // trustworthy. Clear it to force every transaction to re-validate its signature
          // against the fork-chain state.
          for (TransactionCapsule tx : item.getBlk().getTransactions()) {
            tx.setVerified(false);
          }
          applyBlock(item.getBlk().setSwitch(true));
```

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L1240-1271)
```java
  public List<TransactionCapsule> getVerifyTxs(BlockCapsule block) {

    if (pendingTransactions.size() == 0) {
      return block.getTransactions();
    }

    List<TransactionCapsule> txs = new ArrayList<>();
    Map<String, TransactionCapsule> txMap = new HashMap<>();
    Set<String> multiAddresses = new HashSet<>(ownerAddressSet);

    pendingTransactions.forEach(capsule -> {
      String txId = Hex.toHexString(capsule.getTransactionId().getBytes());
      if (isMultiSignTransaction(capsule.getInstance())) {
        String address = Hex.toHexString(capsule.getOwnerAddress());
        multiAddresses.add(address);
      } else {
        txMap.put(txId, capsule);
      }
    });

    block.getTransactions().forEach(capsule -> {
      String address = Hex.toHexString(capsule.getOwnerAddress());
      String txId = Hex.toHexString(capsule.getTransactionId().getBytes());
      if (multiAddresses.contains(address) || !isSameSig(capsule, txMap.get(txId))) {
        txs.add(capsule);
      } else {
        capsule.setVerified(true);
      }
    });

    return txs;
  }
```

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L1301-1301)
```java
        List<TransactionCapsule> txs = getVerifyTxs(block);
```

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L1388-1391)
```java
            long oldSolidNum = getDynamicPropertiesStore().getLatestSolidifiedBlockNum();
            try (ISession tmpSession = revokingStore.buildSession()) {
              applyBlock(newBlock, txs);
              tmpSession.commit();
```
