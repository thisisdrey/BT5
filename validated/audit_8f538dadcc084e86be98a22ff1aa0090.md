Found the analog. This is exactly the "boundary-crossing state confusion" pattern described in the report: `preValidateTransactionSign` runs signature verification for *all* transactions in an incoming block **in parallel, against the pre-block account state**, before any transaction in the block is actually applied. The per-transaction result is cached on the `TransactionCapsule` via `isVerified`/`validateSignature`, and `processTransaction` later trusts that cached flag instead of re-checking the permission set that may have just been mutated by an earlier transaction in the same block.

### Title
Cached signature verification in block processing is validated against stale (pre-block) permission state - (File: framework/src/main/java/org/tron/core/db/Manager.java)

### Summary
`processBlock` calls `preValidateTransactionSign(txs)` to parallel-verify every transaction's signature against the **current head state** before the block's transactions are executed sequentially [1](#0-0) . Each `ValidateSignTask` calls `trx.validateSignature(...)`, which sets `isVerified=true` on the capsule if the current signer weight satisfies the account's permission at that moment [2](#0-1) [3](#0-2) . Later, `processTransaction` (executed sequentially, in-order) calls `trxCap.validateSignature(...)` again, but the `isVerified` guard short-circuits and returns `true` immediately without re-deriving the permission/weight from the state as of that point in the block [4](#0-3) [5](#0-4) .

### Finding Description
This mirrors the Go TLS bug class: a batch of "messages" (here, transactions in a block) is validated/committed at a stage boundary (pre-block state) before the boundary transition (mid-block permission mutation via `AccountPermissionUpdateContract`) actually happens, and later stages of the pipeline (sequential execution) trust the pre-boundary verification result instead of re-validating against the updated state.

Concretely: if transaction A (earlier in block order) is an `AccountPermissionUpdateContract` that revokes/reduces a key's weight or removes it from the account's `Active`/`Owner` permission via `AccountPermissionUpdateActuator.execute` (which directly mutates the `AccountCapsule` and persists it to `AccountStore`) [6](#0-5) , and transaction B (later in the same block, signed by the now-revoked key) was pre-validated in parallel by `preValidateTransactionSign` against the pre-block account state, then B's `isVerified` flag is already `true` by the time the sequential loop in `processBlock`/`processTransaction` reaches it. `validateSignature` in `TransactionCapsule` will not recompute anything because of the `if (!isVerified)` short-circuit [5](#0-4) , so B is treated as validly authorized even though, by execution time, the signer no longer has authority per the current permission/weight/threshold.

### Impact Explanation
A malicious block producer (or an attacker able to influence transaction ordering within a block they produce, or via `getVerifyTxs`/`pushBlock` re-pack paths) could order an `AccountPermissionUpdateContract` before a transaction signed by a key it revokes, and have that later transaction still execute as authorized. This allows unauthorized account operations (e.g., transfers, contract calls, further permission changes) to be committed to the chain using a key that has been removed/de-weighted, in exactly the same block — a concrete "unauthorized account operation" outcome, since the actuator execution path never re-derives authority from the state actually current when B runs.

### Likelihood Explanation
This requires no privileged role beyond ordinary broadcasters/signers coordinating two transactions with a specific ordering that a block producer or the reachable transaction pool (`generateBlock`/`processBlock`) will pack together; it does not require a malicious SR/witness beyond normal block production, and the described state (parallel pre-validation cached via `isVerified`, then trusted without re-check by sequential execution) is present in the mainline `processBlock` path for every block that is not self-generated (`!block.generatedByMyself`) [7](#0-6) .

### Recommendation
Do not let the parallel pre-validation result be trusted for sequential execution correctness across permission-mutating boundaries: either (a) skip caching `isVerified` for any transaction whose signer/owner address is affected by an earlier `AccountPermissionUpdateContract` in the same block (similar to the `multiAddresses` invalidation logic already used in `getVerifyTxs`) [8](#0-7) , or (b) always re-derive `checkWeight`/permission validity at actual execution time in `processTransaction` regardless of the `isVerified` cache when the transaction's owner account was mutated earlier in the same block.

### Proof of Concept
Conceptually (cannot be executed without the full node/test harness):
1. Fund account X with an active-permission key K1 (threshold 1, weight 1).
2. Build TX_A: `AccountPermissionUpdateContract` from X removing K1 from the active permission, signed by X's owner key.
3. Build TX_B: any `TransferContract` from X, signed with K1, placed after TX_A in block ordering.
4. A block producer packs [TX_A, TX_B] into a block; `preValidateTransactionSign` verifies both in parallel against the pre-block state, where K1 is still valid for X, marking TX_B `isVerified=true`.
5. `processBlock`'s sequential loop executes TX_A (revoking K1) then TX_B; TX_B's `validateSignature` short-circuits on the cached `isVerified`, and `AccountPermissionUpdateActuator`/relevant actuator executes TX_B despite K1 no longer being authorized — demonstrating unauthorized execution using a revoked key within the same block.

Note: I was not able to execute this scenario against a live/test node in this session; verification would require running the described block-packing sequence against `ManagerTest`-style test infrastructure (similar to `switchForkShouldResetTransactionSignVerifiedOnNewBranch`, which shows the codebase is already aware `isVerified` caching can go stale across state changes and resets it on fork switch, but not for the intra-block same-block ordering case) [9](#0-8) .

### Citations

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L1240-1268)
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
```

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

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L2668-2692)
```java
  private static class ValidateSignTask implements Callable<Boolean> {

    private TransactionCapsule trx;
    private CountDownLatch countDownLatch;
    private ChainBaseManager manager;

    ValidateSignTask(TransactionCapsule trx, CountDownLatch countDownLatch,
        ChainBaseManager manager) {
      this.trx = trx;
      this.countDownLatch = countDownLatch;
      this.manager = manager;
    }

    @Override
    public Boolean call() throws ValidateSignatureException {
      try {
        trx.validateSignature(manager.getAccountStore(), manager.getDynamicPropertiesStore());
      } catch (ValidateSignatureException e) {
        throw e;
      } finally {
        countDownLatch.countDown();
      }
      return true;
    }
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

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L41-53)
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

**File:** framework/src/test/java/org/tron/core/db/ManagerTest.java (L1776-1845)
```java
  /**
   * A fork switch re-applies the new branch on a rewound, diverged state, so any signature
   * verification cached on those transactions (isVerified) must be cleared to force
   * re-validation against the fork-chain state. Drives a real reorg and asserts that switchFork
   * resets isVerified on the transactions of the branch it switches to.
   */
  @Test
  public void switchForkShouldResetTransactionSignVerifiedOnNewBranch() throws Exception {
    // bootstrap a head with a known witness
    String key = PublicMethod.getRandomPrivateKey();
    byte[] privateKey = ByteArray.fromHexString(key);
    final ECKey ecKey = ECKey.fromPrivate(privateKey);
    byte[] address = ecKey.getAddress();
    ByteString addressByte = ByteString.copyFrom(address);
    chainManager.getAccountStore().put(addressByte.toByteArray(),
        new AccountCapsule(Protocol.Account.newBuilder().setAddress(addressByte).build()));
    WitnessCapsule witnessCapsule = new WitnessCapsule(addressByte);
    chainManager.getWitnessScheduleStore().saveActiveWitnesses(new ArrayList<>());
    chainManager.addWitness(addressByte);
    chainManager.getWitnessStore().put(address, witnessCapsule);
    Block block = blockGenerate.getSignedBlock(
        witnessCapsule.getAddress(), 1533529947843L, privateKey);
    dbManager.pushBlock(new BlockCapsule(block));

    Map<ByteString, String> keys = addTestWitnessAndAccount();
    keys.put(addressByte, key);

    // fund an owner; transfers go owner -> witness 'address' (an existing account)
    ECKey ownerKey = new ECKey(Utils.getRandom());
    byte[] owner = ownerKey.getAddress();
    AccountCapsule ownerAccount = new AccountCapsule(
        Protocol.Account.newBuilder().setAddress(ByteString.copyFrom(owner)).build());
    ownerAccount.setBalance(1_000_000_000L);
    chainManager.getAccountStore().put(owner, ownerAccount);

    long t = 1533529947843L;
    long base = chainManager.getDynamicPropertiesStore().getLatestBlockHeaderNumber();
    long expiration = t + 1_000_000L;

    // common ancestor P (empty) — fork point and tapos reference
    BlockCapsule p = createTestBlockCapsule(t + 3000, base + 1,
        chainManager.getDynamicPropertiesStore().getLatestBlockHeaderHash().getByteString(), keys);
    dbManager.pushBlock(p);

    // old branch: A extends P via the normal path and becomes head
    BlockCapsule a = blockWithTransfer(t + 6000, base + 2, p.getBlockId().getByteString(), keys,
        transfer(owner, address, 1L, p, expiration));
    dbManager.pushBlock(a);
    Assert.assertEquals("control: head should be A after normal extend",
        a.getBlockId(), chainManager.getDynamicPropertiesStore().getLatestBlockHeaderHash());

    // heavier competing branch P -> B1 -> B2 forces switchFork; spy the tx on the branch we
    // switch to and pre-mark it verified to mimic a stale cache computed on a different state
    BlockCapsule b1 = blockWithTransfer(t + 6001, base + 2, p.getBlockId().getByteString(), keys,
        transfer(owner, address, 2L, p, expiration));
    dbManager.pushBlock(b1); // num <= head -> kept in khaosDb, no switch yet

    TransactionCapsule forkTx = transfer(owner, address, 3L, p, expiration);
    forkTx.setVerified(true);
    TransactionCapsule spyTx = spy(forkTx);
    BlockCapsule b2 = blockWithTransfer(t + 9000, base + 3, b1.getBlockId().getByteString(), keys,
        spyTx);
    dbManager.pushBlock(b2); // num > head & parent != head -> triggers switchFork

    Assert.assertEquals("reorg must switch the canonical head to the competing branch (B2)",
        b2.getBlockId(), chainManager.getDynamicPropertiesStore().getLatestBlockHeaderHash());
    // switchFork must clear the cached verification flag on the new branch's transaction so it
    // re-validates against the fork-chain state
    verify(spyTx, atLeastOnce()).setVerified(false);
  }
```
