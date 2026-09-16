### Title
Local mempool (`pendingTransactions`) duplicate-execution allows a single signed transaction to be applied twice within one block interval - ([File: framework/src/main/java/org/tron/core/db/Manager.java])

### Summary
`Manager.validateDup()` — the sole in-band duplicate check invoked from `processTransaction()` on every `pushTransaction()` call — only consults the confirmed-transaction bloom filter (`TransactionCache`) and the persisted `TransactionStore`. It never checks the live, in-memory `pendingTransactions` queue. Because `Wallet.broadcastTransaction()`'s own de-duplication is a bounded LRU `transactionIdCache` (size 100,000, hardcoded), an attacker who floods the node with more distinct valid transactions than that cache size within a single block interval can evict the entry for an earlier transaction and get it accepted a second time while the first copy is still sitting, unexecuted-into-a-block, in `pendingTransactions`. This is a direct structural analog of the CometBFT list/map desynchronization bug: the “list” (`pendingTransactions` queue) and the “map” (`transactionIdCache` / `transactionCache`) are not kept in sync, so the same transaction can be present twice in the packing queue and get executed and packed into a block twice.

### Finding Description
`Manager.pushTransaction()` performs the following sequence for every incoming transaction (broadcast RPC/HTTP, `rePush`, etc.): [1](#0-0) 

The only duplicate-submission guard exercised inside this path is `validateDup()`, called from `processTransaction()`: [2](#0-1) 

`containsTransaction()` only returns `true` if the transaction is already recorded in the confirmed-transaction bloom filter (`transactionCache`, populated only after a transaction is executed — see below) or already persisted in `TransactionStore`. It performs **no lookup against `pendingTransactions`** (the queue of transactions accepted but not yet packed into a block), nor against `pushTransactionQueue`.

`pendingTransactions` itself is a plain `BlockingQueue`/`PriorityBlockingQueue` with no identity/ID-based de-duplication: [3](#0-2) [4](#0-3) 

The `transactionCache`/`TransactionStore` records that `validateDup()` relies on are only written **after** the transaction has already been executed by `trace.exec()`/`trace.finalization()` inside `processTransaction()`: [5](#0-4) 

This means: while a transaction is queued in `pendingTransactions` awaiting block packing, a re-submission of the exact same signed transaction will pass `validateDup()` again (since it hasn't been executed/persisted yet) and will be appended to `pendingTransactions` a second time.

The only thing preventing trivial resubmission at the API layer is the bounded LRU `transactionIdCache` checked in `Wallet.broadcastTransaction()`: [6](#0-5) [7](#0-6) [8](#0-7) 

This cache has a fixed `maximumSize` of `TX_ID_CACHE_SIZE = 100_000`, backed by Guava's `CacheBuilder`, which evicts entries once the size bound is exceeded — exactly the class of "cache_size" bypass described in the CometBFT advisory (`send N more different transactions… N should be higher than the node's configured cache_size`).

Once the same transaction is duplicated in `pendingTransactions`, `generateBlock()` drains the queue and calls `processTransaction(trx, blockCapsule)` for every entry, adding each processed copy to `toBePacked`: [9](#0-8) 

Both copies pass through `processTransaction` independently in separate revoking sessions, each of which re-runs `trace.exec()` (the actuator logic — e.g., balance debit/credit, energy/bandwidth consumption) before the transaction is recorded in `TransactionStore`/`transactionCache`. The resulting block therefore ends up containing the **same transaction ID twice**, with the underlying actuator effects (e.g., a transfer or contract call) applied twice for a single user signature.

### Impact Explanation
A signed transaction (e.g., a `TransferContract`) that is duplicated into `pendingTransactions` gets executed twice by the producing node before being deduplicated at the storage layer. This causes the sender's account to be debited twice (and the receiver credited twice) for a transaction the user authorized only once — an unauthorized/duplicated account operation and unbacked balance change relative to the single signature provided. It also causes bandwidth/energy to be consumed twice and inserts two logically-identical transaction records into the block-generation pipeline for one signed payload, corrupting the accounting for that block (`toBePacked`, balance trace, fee/energy stats). This matches the "unbacked balance" / "unauthorized account operation" impact classes.

### Likelihood Explanation
Reaching this bug requires an unprivileged client to (1) broadcast transaction `tx1` via the standard RPC/HTTP broadcast path, then (2) broadcast more than `TX_ID_CACHE_SIZE` (100,000) additional distinct, validly-signed transactions, then (3) re-broadcast `tx1` again — all before `tx1` is packed into a block (i.e., within roughly one block production interval, ~3s on mainnet). This is the same attack shape as the original CometBFT advisory and is achievable by a single well-resourced unprivileged client capable of generating/broadcasting large volumes of valid signed transactions in a short window; no witness/validator privilege or peer-level trust is required, since the flow is driven purely through the public broadcast RPC.

### Recommendation
Add an identity check against `pendingTransactions` (and `pushTransactionQueue`) inside `containsTransaction()`/`validateDup()` (or immediately upon dequeue in `pushTransaction()`), so a transaction ID already present in the in-flight pending set is rejected before being processed and re-added. Alternatively, maintain a synchronized ID index alongside `pendingTransactions` (mirroring the CometBFT fix's approach of keeping the map and list of sizes in a mutually consistent state) rather than relying solely on the bounded `transactionIdCache`.

### Proof of Concept
1. Sign transaction `tx1` (e.g., a `TransferContract`) and broadcast it via `/wallet/broadcasttransaction` (or gRPC `BroadcastTransaction`). It passes `Wallet.broadcastTransaction()`'s `transactionIdCache` check, is queued in `dbManager.pendingTransactions` by `Manager.pushTransaction()`.
2. Before `tx1` is packed into a block, broadcast more than `TX_ID_CACHE_SIZE` (100,000) other distinct, validly-signed transactions to the same node, causing Guava's `maximumSize`-bounded `transactionIdCache` to evict `tx1`'s entry.
3. Re-broadcast the identical `tx1` payload/signature. `Wallet.broadcastTransaction()`'s cache check no longer finds `tx1`, so it proceeds to `dbManager.pushTransaction(tx1)` again; `processTransaction()`'s `validateDup()` also passes because `tx1` is neither yet in `transactionCache` nor `TransactionStore` (it is still only sitting, unexecuted, in `pendingTransactions`). `tx1` is appended to `pendingTransactions` a second time.
4. When the witness calls `generateBlock()`, both copies of `tx1` are dequeued and each independently run through `processTransaction()`, resulting in the sender being debited (and receiver credited) twice for the amount specified in the single signed transaction, and the produced block containing two entries for the same transaction ID.

### Citations

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L188-188)
```java
  private static final int TX_ID_CACHE_SIZE = 100_000;
```

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L227-230)
```java
  @Getter
  private Cache<Sha256Hash, Boolean> transactionIdCache = CacheBuilder
      .newBuilder().maximumSize(TX_ID_CACHE_SIZE)
      .expireAfterWrite(1, TimeUnit.HOURS).recordStats().build();
```

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L244-252)
```java
  // transactions cache
  private BlockingQueue<TransactionCapsule> pendingTransactions;
  @Getter
  private AtomicInteger shieldedTransInPendingCounts = new AtomicInteger(0);
  // transactions popped
  private List<TransactionCapsule> poppedTransactions =
      Collections.synchronizedList(Lists.newArrayList());
  // the capacity is equal to Integer.MAX_VALUE default
  private BlockingQueue<TransactionCapsule> rePushTransactions;
```

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L505-511)
```java
    if (Args.getInstance().isOpenTransactionSort()) {
      this.pendingTransactions = new PriorityBlockingQueue(2000, downComparator);
      this.rePushTransactions = new PriorityBlockingQueue<>(2000, downComparator);
    } else {
      this.pendingTransactions = new LinkedBlockingQueue<>();
      this.rePushTransactions = new LinkedBlockingQueue<>();
    }
```

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L861-881)
```java
  void validateDup(TransactionCapsule transactionCapsule) throws DupTransactionException {
    if (containsTransaction(transactionCapsule)) {
      throw new DupTransactionException(String.format("dup trans : %s ",
          transactionCapsule.getTransactionId()));
    }
  }

  private boolean containsTransaction(TransactionCapsule transactionCapsule) {
    return containsTransaction(transactionCapsule.getTransactionId().getBytes());
  }


  private boolean containsTransaction(byte[] transactionId) {
    if (transactionCache != null && !transactionCache.has(transactionId)) {
      // using the bloom filter only determines non-existent transaction
      return false;
    }

    return chainBaseManager.getTransactionStore()
        .has(transactionId);
  }
```

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L886-953)
```java
  public boolean pushTransaction(final TransactionCapsule trx)
      throws ValidateSignatureException, ContractValidateException, ContractExeException,
      AccountResourceInsufficientException, DupTransactionException, TaposException,
      TooBigTransactionException, TransactionExpirationException,
      ReceiptCheckErrException, VMIllegalException, TooBigTransactionResultException {

    if (isShieldedTransaction(trx.getInstance()) && !chainBaseManager.getDynamicPropertiesStore()
        .supportShieldedTransaction()) {
      throw new ContractValidateException("ShieldedTransferContract is not supported.");
    }

    if (isExchangeTransaction(trx.getInstance())) {
      throw new ContractValidateException("ExchangeTransactionContract is rejected");
    }

    pushTransactionQueue.add(trx);
    Metrics.gaugeInc(MetricKeys.Gauge.MANAGER_QUEUE, 1,
        MetricLabels.Gauge.QUEUE_QUEUED);
    try {
      if (!trx.validateSignature(chainBaseManager.getAccountStore(),
          chainBaseManager.getDynamicPropertiesStore())) {
        throw new ValidateSignatureException(String.format("trans sig validate failed, id: %s",
            trx.getTransactionId()));
      }

      synchronized (transactionLock) {
        while (true) {
          try {
            if (isBlockWaitingLock()) {
              TimeUnit.MILLISECONDS.sleep(SLEEP_FOR_WAIT_LOCK);
            } else {
              break;
            }
          } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            logger.debug("The wait has been interrupted.");
          }
        }
        synchronized (this) {
          if (isShieldedTransaction(trx.getInstance())
                  && shieldedTransInPendingCounts.get() >= shieldedTransInPendingMaxCounts) {
            return false;
          }
          if (!session.valid()) {
            session.setValue(revokingStore.buildSession());
          }

          try (ISession tmpSession = revokingStore.buildSession()) {
            processTransaction(trx, null);
            trx.setTrxTrace(null);
            pendingTransactions.add(trx);
            Metrics.gaugeInc(MetricKeys.Gauge.MANAGER_QUEUE, 1,
                    MetricLabels.Gauge.QUEUE_PENDING);
            tmpSession.merge();
          }
          if (isShieldedTransaction(trx.getInstance())) {
            shieldedTransInPendingCounts.incrementAndGet();
          }
        }
      }
    } finally {
      if (pushTransactionQueue.remove(trx)) {
        Metrics.gaugeInc(MetricKeys.Gauge.MANAGER_QUEUE, -1,
            MetricLabels.Gauge.QUEUE_QUEUED);
      }
    }
    return true;
  }
```

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L1578-1586)
```java
    trace.finalization();
    if (getDynamicPropertiesStore().supportVM()) {
      trxCap.setResult(trace.getTransactionContext());
    }
    chainBaseManager.getTransactionStore().put(trxCap.getTransactionId().getBytes(), trxCap);

    Optional.ofNullable(transactionCache)
        .ifPresent(t -> t.put(trxCap.getTransactionId().getBytes(),
            new BytesCapsule(ByteArray.fromLong(trxCap.getBlockNum()))));
```

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L1667-1756)
```java
    while (pendingTransactions.size() > 0 || rePushTransactions.size() > 0) {
      boolean fromPending = false;
      TransactionCapsule trx;
      if (pendingTransactions.size() > 0) {
        trx = pendingTransactions.peek();
        if (isSort) {
          TransactionCapsule trxRepush = rePushTransactions.peek();
          if (trxRepush == null || trx.getOrder() >= trxRepush.getOrder()) {
            fromPending = true;
          } else {
            trx = rePushTransactions.poll();
            Metrics.gaugeInc(MetricKeys.Gauge.MANAGER_QUEUE, -1,
                MetricLabels.Gauge.QUEUE_REPUSH);
          }
        } else {
          fromPending = true;
        }
      } else {
        trx = rePushTransactions.poll();
        Metrics.gaugeInc(MetricKeys.Gauge.MANAGER_QUEUE, -1,
            MetricLabels.Gauge.QUEUE_REPUSH);
      }

      if (fromPending) {
        pendingTransactions.poll();
        Metrics.gaugeInc(MetricKeys.Gauge.MANAGER_QUEUE, -1,
                MetricLabels.Gauge.QUEUE_PENDING);
      }

      if (trx == null) {
        //  transaction may be removed by rePushLoop.
        logger.warn("Trx is null, fromPending: {}, pending: {}, repush: {}.",
                fromPending, pendingTransactions.size(), rePushTransactions.size());
        continue;
      }
      if (System.currentTimeMillis() > timeout) {
        logger.warn("Processing transaction time exceeds the producing time {}.",
            System.currentTimeMillis());
        break;
      }

      // check the block size
      long trxPackSize = trx.computeTrxSizeForBlockMessage();
      if ((currentSize + trxPackSize)
          > ChainConstant.BLOCK_SIZE) {
        postponedTrxCount++;
        continue; // try pack more small trx
      }
      //shielded transaction
      Transaction transaction = trx.getInstance();
      if (isShieldedTransaction(transaction)
          && shieldedTransCounts.incrementAndGet() > SHIELDED_TRANS_IN_BLOCK_COUNTS) {
        continue;
      }
      //multi sign transaction
      byte[] owner = trx.getOwnerAddress();
      String ownerAddress = ByteArray.toHexString(owner);
      if (accountSet.contains(ownerAddress)) {
        continue;
      } else {
        if (isMultiSignTransaction(transaction)) {
          accountSet.add(ownerAddress);
        }
      }

      if (isExchangeTransaction(transaction)) {
        continue;
      }

      if (ownerAddressSet.contains(ownerAddress)) {
        trx.setVerified(false);
      }
      // apply transaction
      try (ISession tmpSession = revokingStore.buildSession()) {
        accountStateCallBack.preExeTrans();
        processTransaction(trx, blockCapsule);
        accountStateCallBack.exeTransFinish();
        tmpSession.merge();
        toBePacked.add(trx);
        currentSize += trxPackSize;
        if (fromPending) {
          logSize[2] += 1;
        } else {
          logSize[3] += 1;
        }
      } catch (Exception e) {
        logger.warn("Process trx {} failed when generating block {}, {}.", trx.getTransactionId(),
            blockCapsule.getNum(), e.getMessage());
      }
    }
```

**File:** framework/src/main/java/org/tron/core/Wallet.java (L558-566)
```java
      if (trxCacheEnable) {
        if (dbManager.getTransactionIdCache().getIfPresent(txID) != null) {
          logger.warn("Broadcast transaction {} has failed, it already exists.", txID);
          return builder.setResult(false).setCode(response_code.DUP_TRANSACTION_ERROR)
              .setMessage(ByteString.copyFromUtf8("Transaction already exists.")).build();
        } else {
          dbManager.getTransactionIdCache().put(txID, true);
        }
      }
```
