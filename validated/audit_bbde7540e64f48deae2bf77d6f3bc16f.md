### Title
Non-transactional zk-proof verification cache in `ZKProofStore` allows a permanently-cached proof result to survive rollback of the transaction/block that produced it - ([File: actuator/src/main/java/org/tron/core/actuator/ShieldedTransferActuator.java])

### Summary
`ShieldedTransferActuator.checkProof()` short-circuits expensive zk-SNARK verification of a `ShieldedTransferContract` by first checking `ZKProofStore` for a cached verdict keyed only by `tx.getTransactionId()`. This is functionally the same bug class as ALPINE-CVE-2024-0853: a verification result is cached and reused on a later "connection" (here, a later re-validation of the same transaction id) even though the earlier verification's outcome was never committed as part of the atomic state transition that depended on it.

### Finding Description
`checkProof()` in [1](#0-0)  looks up `chainBaseManager.getProofStore()` by transaction id and, if a cached `true` is found, returns immediately without re-running `librustzcashSaplingCheckSpend`/`CheckOutput`/`FinalCheck`. Only if the crypto checks succeed does it write the cache via `recordProof(tx.getTransactionId(), true)` at [2](#0-1) .

The critical defect is that `ZKProofStore` is a plain `TronDatabase`, not a `TronStoreWithRevoking` participant: [3](#0-2)  writes straight to `dbSource.putData(...)`. Every other piece of state a shielded transfer touches (nullifiers, merkle tree, account balances, `totalShieldedPoolValue`) is mutated through stores that participate in `revokingStore`/`ISession`, so those changes are rolled back cleanly whenever the enclosing `tmpSession` in `Manager.pushTransaction`/`processTransaction` is not merged (e.g. `ContractExeException`, `AccountResourceInsufficientException`, a later actuator failing, or a `switchFork` rollback of an entire branch as seen in [4](#0-3) ). `ZKProofStore.put()` has no such rollback path — once `recordProof` runs, the "proof valid" bit for that transaction id is permanent, regardless of whether the transaction, block, or fork branch that triggered it is ultimately discarded.

This mirrors the curl advisory precisely: curl kept a session-ID/verify-status result cached and trusted it on a subsequent transfer even though the state that produced it (OCSP stapling check) was not durable/valid; here java-tron keeps a "zk-proof verified" bit cached and trusted on a subsequent re-validation even though the state (block/branch) in which it was produced was rolled back.

### Impact Explanation
Because the cache write bypasses the revoking/session isolation that the rest of `Manager`'s transaction pipeline relies on for correctness, a `ZKProofStore` entry can become desynchronized from the canonical chain state that was supposed to gate it (nullifier set, merkle root set, shielded pool balance), all of which *are* correctly rolled back. Any later re-validation of a transaction bearing the same id (re-push after a reorg discards the block that contained it, retry from `rePushTransactions`, or reprocessing during `switchFork`) will skip real zk-proof crypto validation and rely solely on the stale cached bit, even though the reverted state that the actuator otherwise re-checks (nullifier-spent status, merkle anchor existence) is re-evaluated correctly but the SNARK/binding-signature/value-balance check itself is not. This breaks the intended "each re-validation is independent and non-cacheable across chain-state changes" invariant that the rest of the codebase enforces for signature verification (see the `isVerified` reset added specifically for `switchFork`, cited below) — but no equivalent reset/invalidation exists for `ZKProofStore`.

### Likelihood Explanation
Reaching this code only requires broadcasting a `ShieldedTransferContract` transaction, which is a permissionless action available to any signed-transaction sender once shielded transfers are enabled (`supportShieldedTransaction()`). Triggering the divergent-cache condition requires a chain reorg or transaction re-processing event that discards the branch in which a shielded transfer's proof was first validated — reorgs are a normal, attacker-influenceable network condition (a malicious/competing SR producing a heavier fork), and `switchFork` is exercised routinely, as evidenced by the extensive reorg test coverage that Manager already carries for the analogous signature-cache problem ( [5](#0-4) , [6](#0-5) ).

### Recommendation
Make `ZKProofStore` participate in the same revoking/session mechanism as `NullifierStore`, `MerkleContainer`, and the dynamic-properties/account stores it is checked alongside (i.e., extend `TronStoreWithRevoking` instead of `TronDatabase`, or otherwise gate `recordProof` writes on the enclosing session actually being merged/committed). Alternatively, invalidate/clear any `ZKProofStore` entries recorded during blocks that are discarded by `switchFork`/`eraseBlock`, the same way `TransactionCapsule.setVerified(false)` is explicitly reset for the new branch.

### Proof of Concept
Conceptual reproduction (concrete PoC requires a running two-branch reorg harness, similar to existing `ManagerTest#switchForkShouldResetTransactionSignVerifiedOnNewBranch`):
1. Enable shielded transactions (`saveAllowShieldedTransaction(1)`) and build a valid `ShieldedTransferContract` transaction `T` with a genuinely valid zk proof.
2. Include `T` in block `B1` on branch A; `ShieldedTransferActuator.checkProof()` succeeds and calls `recordProof(T.txId, true)`, writing directly to `ZKProofStore`'s underlying LevelDB/RocksDB (bypassing the `tmpSession`).
3. Force a reorg to a heavier competing branch B that does not contain `B1` (as done by `dbManager.pushBlock(b1); dbManager.pushBlock(b2)` in the existing reorg tests), causing `switchFork` to roll back `B1`'s nullifier/merkle/balance state via `revokingStore`, but `ZKProofStore`'s entry for `T.txId` remains on disk.
4. Re-submit/re-process `T` (e.g., via `rePushTransactions` or a subsequent block on the new canonical branch). `checkProof()` finds `proofStore.has(T.txId) == true` and returns immediately without re-running `librustzcashSaplingCheckSpend/CheckOutput/FinalCheck`, demonstrating that the zk-proof crypto check is skipped for a transaction whose gating state was rolled back — the exact "cached verification bypassing re-check" pattern from ALPINE-CVE-2024-0853.

Note: I could not fully trace whether any additional guard (e.g., duplicate-transaction rejection via `TransactionCache`/`TransactionStore.has`) prevents `T` from being reprocessed a second time after the reorg in all code paths; this would need to be confirmed by tracing `Manager.getRePushTransactions()`/`rePushLoop` and `containsTransaction` in a live/dynamic session, which is outside what static inspection here could fully confirm.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ShieldedTransferActuator.java (L275-285)
```java
  private void checkProof(List<SpendDescription> spendDescriptions,
      List<ReceiveDescription> receiveDescriptions, long fee) throws ZkProofValidateException {
    DynamicPropertiesStore dynamicStore = chainBaseManager.getDynamicPropertiesStore();
    ZKProofStore proofStore = chainBaseManager.getProofStore();
    if (proofStore.has(tx.getTransactionId().getBytes())) {
      if (proofStore.get(tx.getTransactionId().getBytes())) {
        return;
      } else {
        throw new ZkProofValidateException("record is fail, skip proof", false);
      }
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ShieldedTransferActuator.java (L357-363)
```java
    recordProof(tx.getTransactionId(), true);
  }

  private void recordProof(Sha256Hash tid, boolean result) {
    ZKProofStore proofStore = chainBaseManager.getProofStore();
    proofStore.put(tid.getBytes(), result);
  }
```

**File:** chainbase/src/main/java/org/tron/core/store/ZKProofStore.java (L8-20)
```java
@Component
public class ZKProofStore extends TronDatabase<Boolean> {

  @Autowired
  public ZKProofStore(ApplicationContext ctx) {
    super("zkProof");
  }

  @Override
  public void put(byte[] key, Boolean item) {
    byte[] b = {(byte) (item.booleanValue() ? 0x01 : 0x00)};
    dbSource.putData(key, b);
  }
```

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L1143-1157)
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
          tmpSession.commit();
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
