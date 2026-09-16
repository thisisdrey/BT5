### Title
Unsynchronized concurrent access to `Manager.ownerAddressSet` causes race condition / potential node hang or crash - (File: `framework/src/main/java/org/tron/core/db/Manager.java`)

### Summary
`Manager` keeps a plain, non-thread-safe `HashSet<String> ownerAddressSet` that tracks addresses of pending multi-sign transactions [1](#0-0) . This set is mutated from the transaction-broadcast path (`pushTransaction` → `processTransaction`) under a `synchronized (this)` block [2](#0-1) [3](#0-2) , but it is read from `generateBlock`, a method the block-producing thread runs **without** taking that same `this` lock [4](#0-3) [5](#0-4) . This is directly analogous to the reported kernel bug class: a shared list/collection mutated from one execution context and consumed from another without a protecting lock, leading to memory-safety/availability failures (there, a NULL dereference from an unsynchronized `ctx_list`; here, undefined behavior on a concurrently-mutated `java.util.HashSet`).

### Finding Description
`ownerAddressSet` is declared as an ordinary `HashSet` (not `Collections.synchronizedSet` nor a concurrent collection) [1](#0-0) . Writers to this set:
- `processTransaction()` adds to it under `synchronized (this)` (called from `pushTransaction()`, which any unprivileged network peer or API client can trigger by broadcasting a multi-sign `AccountPermissionUpdateContract`) [6](#0-5) [2](#0-1) .

Readers include:
- `getVerifyTxs()`, called from `pushBlock()` inside `synchronized (this)` — this one is properly protected [7](#0-6) [8](#0-7) .
- `generateBlock()`, which reads `ownerAddressSet.contains(ownerAddress)` while packing transactions, but the method itself is **not** `synchronized` and does not otherwise take the `this` monitor around that read [4](#0-3) [5](#0-4) .
- `filterOwnerAddress()`, also unsynchronized [9](#0-8) .

Because a witness node continuously runs `generateBlock()` on its block-production thread while simultaneously accepting broadcast transactions from arbitrary network peers/API clients via `pushTransaction()`, the JVM can execute a `HashSet.contains()` read concurrently with a `HashSet.add()` write with no shared lock. `HashSet`/`HashMap` are documented to be unsafe for concurrent modification: concurrent structural modification (bucket resize during `add`) racing with an unsynchronized read can corrupt the internal bucket/linked-list structure. This is a long-documented Java hazard that can produce an infinite loop (100% CPU spin, effectively a livelock) or a `ConcurrentModificationException`/corrupted iteration depending on JVM version and access pattern — mirroring the "missing lock on shared list → crash from concurrent access" root cause of CVE-2024-35919.

### Impact Explanation
If triggered, the race can pin a CPU core in an infinite loop inside `HashSet` internals (well-known JDK 7/8 HashMap resize livelock class of bug) or throw an unexpected `ConcurrentModificationException`/produce corrupted state during block generation, stalling or crashing the witness/SR block-production thread. Because `generateBlock()` is on the critical path for producing blocks, a stalled or crashed block-production thread on a witness node is a liveness/availability impact reachable purely by normal, unprivileged transaction broadcast traffic — no witness/SR misbehavior is required to trigger it (only ordinary multi-sign transactions being broadcast while that node happens to also be producing blocks, which is normal operation for any SR node).

### Likelihood Explanation
Any account holder can submit an `AccountPermissionUpdateContract` transaction via `broadcastTransaction` (Wallet/gRPC or HTTP API), which is the trigger for the `ownerAddressSet.add()` write path in `processTransaction()` [2](#0-1) [10](#0-9) . This write races naturally against every block-production cycle on the same node (every ~3s per SR), so the race window is exercised continuously in normal operation, not just under an adversarial scenario — no special privileges or malicious SR/witness collusion required.

### Recommendation
Guard all reads and writes of `ownerAddressSet` with the same lock (e.g., wrap the field in `Collections.synchronizedSet(new HashSet<>())` and synchronize iteration, or acquire `synchronized (this)` around the `contains()`/`add()` calls in `generateBlock()` and `filterOwnerAddress()` as is already done in `pushTransaction()`/`getVerifyTxs()`), or replace it with a `ConcurrentHashMap`-backed set.

### Proof of Concept
1. Start a witness node so its `generateBlock()` loop runs continuously and reads `ownerAddressSet.contains(...)` on each cycle while iterating `pendingTransactions`/`rePushTransactions` [11](#0-10) .
2. From an unprivileged client, continuously broadcast valid `AccountPermissionUpdateContract` transactions from many distinct owner addresses via the public API, each of which triggers `pushTransaction()` → `processTransaction()` → `ownerAddressSet.add(...)` under `synchronized (this)` [12](#0-11) [2](#0-1) .
3. Because the writer holds `this` but the reader in `generateBlock()` does not, the JVM's `HashSet` internal state can be observed mid-resize by the unsynchronized reader, risking an infinite loop in `HashSet` bucket traversal or an exception propagating out of `generateBlock()`, stalling or aborting block production on that node.

### Citations

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L235-235)
```java
  private Set<String> ownerAddressSet = new HashSet<>();
```

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L886-944)
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
```

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L1240-1248)
```java
  public List<TransactionCapsule> getVerifyTxs(BlockCapsule block) {

    if (pendingTransactions.size() == 0) {
      return block.getTransactions();
    }

    List<TransactionCapsule> txs = new ArrayList<>();
    Map<String, TransactionCapsule> txMap = new HashMap<>();
    Set<String> multiAddresses = new HashSet<>(ownerAddressSet);
```

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L1276-1285)
```java
  public void pushBlock(final BlockCapsule block)
      throws ValidateSignatureException, ContractValidateException, ContractExeException,
      UnLinkedBlockException, ValidateScheduleException, AccountResourceInsufficientException,
      TaposException, TooBigTransactionException, TooBigTransactionResultException,
      DupTransactionException, TransactionExpirationException,
      BadNumberBlockException, BadBlockException, NonCommonBlockException,
      ReceiptCheckErrException, VMIllegalException, ZksnarkException, EventBloomException {
    setBlockWaitLock(true);
    try {
      synchronized (this) {
```

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L1600-1602)
```java
    if (isMultiSignTransaction(trxCap.getInstance())) {
      ownerAddressSet.add(ByteArray.toHexString(trxCap.getOwnerAddress()));
    }
```

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L1631-1631)
```java
  public BlockCapsule generateBlock(Miner miner, long blockTime, long timeout) {
```

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L1667-1738)
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

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L1787-1796)
```java
  private boolean isMultiSignTransaction(Transaction transaction) {
    Contract contract = transaction.getRawData().getContract(0);
    switch (contract.getType()) {
      case AccountPermissionUpdateContract: {
        return true;
      }
      default:
    }
    return false;
  }
```
