### Title
Unbounded in-memory growth of `Manager.ownerAddressSet` via repeated `AccountPermissionUpdateContract` broadcasts causes permanent memory leak - (File: `framework/src/main/java/org/tron/core/db/Manager.java`)

### Summary
The Mosquitto CVE is a broker-side memory leak where records keyed by attacker-controlled identifiers (duplicate QoS2 message IDs) are added to an internal structure but never freed due to a mishandled error path. `java-tron`'s `Manager` class contains an analogous pattern: a `HashSet<String>` keyed by transaction owner address is populated for every accepted multisign-permission transaction but is never pruned, bounded, or expired, unlike its sibling caches in the same class.

### Finding Description
`Manager` declares `private Set<String> ownerAddressSet = new HashSet<>();` [1](#0-0) . Inside `processTransaction`, whenever a transaction is identified as a multisign transaction (currently only `AccountPermissionUpdateContract`), its owner address (hex-encoded) is unconditionally added to this set:
```
if (isMultiSignTransaction(trxCap.getInstance())) {
  ownerAddressSet.add(ByteArray.toHexString(trxCap.getOwnerAddress()));
}
``` [2](#0-1) 

`processTransaction` is invoked both for transactions packed into blocks and for every transaction accepted through `pushTransaction`, which is the path reached by any unprivileged client broadcasting a signed transaction via `Wallet.broadcastTransaction` / gRPC / HTTP APIs [3](#0-2) .

`ownerAddressSet` is only ever read (via `contains`) inside `getVerifyTxs` and `generateBlock` to decide whether a cached signature-verification flag can be trusted [4](#0-3) [5](#0-4) . Across the entire `Manager` class there is no code path that calls `ownerAddressSet.remove(...)` or `ownerAddressSet.clear()` in production logic — the only `clear()`/`add()` calls on this field found are in unit tests that manipulate it via reflection [6](#0-5) .

This is in stark contrast to every other per-transaction bookkeeping structure in the same class:
- `pendingTransactions` / `rePushTransactions` are drained and subject to a configurable `pendingTransactionTimeout` via `PendingManager.close()` [7](#0-6) .
- `transactionIdCache` is a Guava `Cache` bounded to `TX_ID_CACHE_SIZE = 100_000` with a 1-hour `expireAfterWrite` [8](#0-7) .

`ownerAddressSet`, however, has no size cap and no TTL — every unique owner address that ever submits an `AccountPermissionUpdateContract` is retained in memory for the lifetime of the process. Because any account (including freshly created, cheap accounts) can trigger this contract type, an attacker can drive unbounded growth of this in-memory `Set<String>` simply by broadcasting a stream of transactions from many distinct addresses, mirroring the Mosquitto pattern of a broker retaining state keyed by attacker-supplied identifiers with no cleanup path.

### Impact Explanation
Continuous broadcasting of `AccountPermissionUpdateContract` transactions from distinct owner addresses causes `Manager.ownerAddressSet` to grow without bound for the entire uptime of the node, with no eviction policy. Over time this results in unconstrained JVM heap consumption, eventually leading to `OutOfMemoryError`, GC pressure, and node crash/halt — impacting availability of a full node/validator, which can affect block production and consensus liveness of a supermajority of nodes if broadly exploited across the network.

### Likelihood Explanation
The trigger is a single, otherwise-valid signed transaction of type `AccountPermissionUpdateContract`, reachable by any account (owner-address-controlled, permissionless) through the standard broadcast path (`Wallet.broadcastTransaction` → `Manager.pushTransaction` → `processTransaction`). The only cost is the `updateAccountPermissionFee` charged per transaction and the cost of funding a distinct account per entry; there is no rate limit tied specifically to this leak, and the set is never cleaned regardless of fee payment. This makes the bug straightforward to trigger repeatedly and cheaply relative to the node's memory budget over a long enough campaign.

### Recommendation
Bound `ownerAddressSet` the same way `transactionIdCache` is bounded — e.g. replace it with a Guava `Cache<String, Boolean>` with a `maximumSize` and `expireAfterWrite`/`expireAfterAccess`, or explicitly remove owner addresses once their associated multisign-permission transaction has been consumed/expired from `pendingTransactions`/`rePushTransactions`. Add metrics/alarming on its size to detect anomalous growth.

### Proof of Concept
1. Fund a batch of throwaway accounts with the minimal balance required to pay `updateAccountPermissionFee`.
2. From each account, broadcast a valid `AccountPermissionUpdateContract` transaction (this is a normal, permissionless operation any wallet can perform).
3. Each accepted transaction causes `Manager.processTransaction` to add the (unique) owner address hex string to `ownerAddressSet` via `ownerAddressSet.add(ByteArray.toHexString(trxCap.getOwnerAddress()))` [2](#0-1) .
4. Repeat step 2 continuously from newly generated addresses; observe (e.g., via heap dump or JVM memory monitoring) that `ownerAddressSet`'s size increases monotonically with no corresponding decrease, regardless of node uptime, block processing, or transaction expiration — unlike `pendingTransactions`, `rePushTransactions`, or `transactionIdCache`, whose sizes are bounded/self-limiting.
5. Sustained execution over time drives heap usage upward, eventually leading to memory exhaustion and node instability/crash.

### Citations

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L227-230)
```java
  @Getter
  private Cache<Sha256Hash, Boolean> transactionIdCache = CacheBuilder
      .newBuilder().maximumSize(TX_ID_CACHE_SIZE)
      .expireAfterWrite(1, TimeUnit.HOURS).recordStats().build();
```

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L235-235)
```java
  private Set<String> ownerAddressSet = new HashSet<>();
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

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L1600-1602)
```java
    if (isMultiSignTransaction(trxCap.getInstance())) {
      ownerAddressSet.add(ByteArray.toHexString(trxCap.getOwnerAddress()));
    }
```

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L1721-1738)
```java
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
```

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L1779-1785)
```java
  private void filterOwnerAddress(TransactionCapsule transactionCapsule, Set<String> result) {
    byte[] owner = transactionCapsule.getOwnerAddress();
    String ownerAddress = ByteArray.toHexString(owner);
    if (ownerAddressSet.contains(ownerAddress)) {
      result.add(ownerAddress);
    }
  }
```

**File:** framework/src/test/java/org/tron/core/db/ManagerTest.java (L907-928)
```java
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
```

**File:** framework/src/main/java/org/tron/core/db/PendingManager.java (L23-60)
```java
  @Override
  public void close() {

    long now = System.currentTimeMillis();
    Iterator<TransactionCapsule> iterator = dbManager.getRePushTransactions().iterator();
    while (iterator.hasNext()) {
      TransactionCapsule tx = iterator.next();
      if (now - tx.getTime() > timeout) {
        iterator.remove();
        Metrics.gaugeInc(MetricKeys.Gauge.MANAGER_QUEUE, -1,
            MetricLabels.Gauge.QUEUE_REPUSH);
        Metrics.counterInc(MetricKeys.Counter.TXS, 1,
            MetricLabels.Counter.TXS_FAIL, MetricLabels.Counter.TXS_FAIL_TIMEOUT);
        if (Args.getInstance().isOpenPrintLog()) {
          logger.warn("Timeout remove tx from repush, txId: {}.", tx.getTransactionId());
        }
      }
    }

    for (TransactionCapsule tx : dbManager.getPendingTransactions()) {
      txIteration(tx);
    }

    dbManager.getPendingTransactions().clear();
    Metrics.gaugeSet(MetricKeys.Gauge.MANAGER_QUEUE, 0,
        MetricLabels.Gauge.QUEUE_PENDING);
    for (TransactionCapsule tx : dbManager.getPoppedTransactions()) {
      tx.setTime(System.currentTimeMillis());
      txIteration(tx);
    }
    dbManager.getPoppedTransactions().clear();
    Metrics.gaugeSet(MetricKeys.Gauge.MANAGER_QUEUE, 0,
        MetricLabels.Gauge.QUEUE_POPPED);
    if (Args.getInstance().isOpenPrintLog()) {
      logger.info("Pending tx size: {}.", dbManager.getRePushTransactions().size());
    }

  }
```
