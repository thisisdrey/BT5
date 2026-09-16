### Title
Stale permission/signature validation cache allows revoked keys to authorize transactions within the same block - ([File: framework/src/main/java/org/tron/core/db/Manager.java])

### Summary
When a node applies a block received from a peer, `Manager.processBlock()` first calls `preValidateTransactionSign(txs)` to verify every transaction's signature/permission weight in parallel against the account store state as it exists *before any transaction in that block has executed*. Later, each transaction is executed sequentially via `processTransaction()`, which calls `trxCap.validateSignature(...)` again — but this second call is a no-op because `TransactionCapsule` caches the result in the `isVerified` flag set by the first check. If an earlier transaction in the same block changes an account's active/owner permission (via `AccountPermissionUpdateContract`), a later transaction from that account signed under the now-superseded permission set is still accepted as validly signed, because its signature/weight was only ever checked against the stale pre-block permission snapshot.

### Finding Description
`Manager.processBlock()` performs a parallel signature pre-check across the whole transaction list before any of them are applied: [1](#0-0) 

Then it iterates and executes transactions one at a time: [2](#0-1) 

Inside `processTransaction`, the signature/permission check is invoked again: [3](#0-2) 

But `TransactionCapsule.validateSignature()`/`validatePubSignature()` short-circuits whenever `isVerified` is already `true`, which it is after `preValidateTransactionSign` ran: [4](#0-3) [5](#0-4) 

Permission changes made by `AccountPermissionUpdateContract` are applied immediately and synchronously to the `AccountStore` when its actuator executes: [6](#0-5) [7](#0-6) 

Consequently, for a block containing transaction A (an `AccountPermissionUpdateContract` that revokes/replaces a key on account X) at position `i`, followed later by transaction B (any operation signed by the *old* key of account X) at position `j > i`: both A and B were signature-checked by `preValidateTransactionSign` against the account store state as it existed *before the block began executing* — i.e., before A had actually run. When B is subsequently executed inside the block-application loop, `validateSignature()` is skipped due to the `isVerified` cache, so B's signature is never re-checked against the post-A permission state. The old (now revoked) key is still accepted as authorizing B, even though it should no longer have permission at that point in the chain.

This is the same class of bug as the CVE analog: a security decision (which permission/domain applies) is made from a context snapshot that becomes stale before the decision is actually acted upon, and the code fails to re-check at the point of use — a time-of-check/time-of-use gap that lets an operation execute under the wrong (stale) authorization context.

### Impact Explanation
This breaks the intended atomicity/ordering guarantee of key/permission rotation. Any account owner (e.g., a custodian or exchange doing an emergency key rotation to revoke a compromised key) cannot rely on the revocation taking effect for transactions ordered later within the *same* block: a transaction signed by the just-revoked key can still be executed successfully, i.e., an unauthorized account operation is performed using credentials that should already be invalid. This affects every full node applying a synced/relayed block (`!block.generatedByMyself` path), which is the default path used by the vast majority of nodes in the network, so it is a deterministic, network-wide correctness issue, not merely a local anomaly.

### Likelihood Explanation
Reachable by any unprivileged account holder who can get two ordinary, self-signed transactions (a permission update and a follow-up contract call) included in the same block in the right relative order — no special network position, elevated privilege, or witness/committee role is required. Block producers routinely batch multiple pending transactions from the same sender into one block, so this ordering is easily achievable without any cooperation from a malicious validator.

### Recommendation
Do not treat the `isVerified` flag as valid across the whole-block pre-validation and the per-transaction execution phases when block state can change permissions mid-block. Either: (1) re-run permission/weight validation against the current account store state inside `processTransaction()` for `AccountPermissionUpdateContract`-sensitive paths regardless of the cached flag, or (2) restrict `preValidateTransactionSign`'s parallel check to pure cryptographic signature recovery (which is state-independent) and always perform the permission/weight/threshold comparison (which is state-dependent) serially inside `processTransaction()` against the up-to-date store.

### Proof of Concept
1. Account X has active permission P1 containing key K1.
2. Construct transaction A: `AccountPermissionUpdateContract` from X, signed by K1 (via P1), replacing active permission with P2 (removing K1, adding K2).
3. Construct transaction B: any contract call from X, signed with K1 under permission id referencing P1.
4. Get both A and B included in the same block with A at an earlier index than B (e.g., submit both to the same block-producing node in that relative order).
5. When any peer node receives and applies this block via `Manager.processBlock()`, `preValidateTransactionSign` validates B's signature against the pre-block state (where P1/K1 is still valid), setting `isVerified=true` on B's `TransactionCapsule`.
6. During the sequential execution loop, A executes first and revokes K1 from X's permissions via `AccountPermissionUpdateActuator.execute()`.
7. B then executes; `processTransaction()` calls `trxCap.validateSignature(...)`, which short-circuits due to `isVerified == true` and never re-checks that K1 is no longer part of X's permission set — B is accepted and executed despite being signed by a revoked key.

### Citations

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L1542-1546)
```java
    if (!trxCap.validateSignature(chainBaseManager.getAccountStore(),
        chainBaseManager.getDynamicPropertiesStore())) {
      throw new ValidateSignatureException(
          String.format(" %s transaction signature validate failed", txId));
    }
```

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L1866-1874)
```java
    //parallel check sign
    if (!block.generatedByMyself) {
      try {
        preValidateTransactionSign(txs);
      } catch (InterruptedException e) {
        logger.error("Parallel check sign interrupted exception! block info: {}.", block, e);
        Thread.currentThread().interrupt();
      }
    }
```

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L1884-1902)
```java
      for (TransactionCapsule transactionCapsule : block.getTransactions()) {
        rejectExchangeTransaction(transactionCapsule.getInstance());
        if (chainBaseManager.getDynamicPropertiesStore().allowConsensusLogicOptimization()
            && transactionCapsule.retCountIsGreatThanContractCount()) {
          throw new BadBlockException(String.format("The result count %d of this transaction %s is "
                  + "greater than its contract count %d", transactionCapsule.getRetCount(),
              transactionCapsule.getTransactionId(), transactionCapsule.getContractCount()));
        }
        transactionCapsule.setBlockNum(num);
        if (block.generatedByMyself) {
          transactionCapsule.setVerified(true);
        }
        accountStateCallBack.preExeTrans();
        TransactionInfo result = processTransaction(transactionCapsule, block);
        accountStateCallBack.exeTransFinish();
        if (Objects.nonNull(result)) {
          results.add(result);
        }
      }
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

**File:** chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java (L698-719)
```java
  public boolean validateSignature(AccountStore accountStore,
      DynamicPropertiesStore dynamicPropertiesStore) throws ValidateSignatureException {
    if (!isVerified) {
      //Do not support multi contracts in one transaction
      Transaction.Contract contract = this.getInstance().getRawData().getContract(0);
      if (contract.getType() != ContractType.ShieldedTransferContract) {
        validatePubSignature(accountStore, dynamicPropertiesStore);
      } else {  //ShieldedTransfer
        byte[] owner = getOwnerAddress();
        if (!ArrayUtils.isEmpty(owner)) { //transfer from transparent address
          validatePubSignature(accountStore, dynamicPropertiesStore);
        } else { //transfer from shielded address
          if (this.transaction.getSignatureCount() > 0) {
            throw new ValidateSignatureException("there should be no signatures signed by "
                    + "transparent address when transfer from shielded address");
          }
        }
      }
      isVerified = true;
    }
    return true;
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L41-52)
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
