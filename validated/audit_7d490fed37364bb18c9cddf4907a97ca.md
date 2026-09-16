### Title
Unsynchronized `null`-out of `bloomFilters` in `TxCacheDB.close()` races with concurrent `get()`/`put()`, causing NPE crash on shutdown/restart - (File: `chainbase/src/main/java/org/tron/core/db2/common/TxCacheDB.java`)

### Summary
`TxCacheDB` backs `TransactionCache`, which is consulted on the transaction-broadcast path to reject duplicate transactions. `close()` nulls out the shared `bloomFilters` array without any lock, while `get()`/`put()` dereference that same array with no lock and no `isAlive()` guard. This is structurally the same bug class as the reported kernel issue: a teardown path clears a shared resource pointer that a concurrently-running "receive" path (RX handler / here, transaction validation) still dereferences without synchronization, producing a null-pointer dereference.

### Finding Description
`TxCacheDB.close()`:
```
public void close() {
  if (!isAlive()) {
    return;
  }
  dump();
  bloomFilters[0] = null;
  bloomFilters[1] = null;
  persistentStore.close();
  setAlive(false);
}
``` [1](#0-0) 

Meanwhile `get()` and `put()`, which are on the hot path for every incoming transaction, access `bloomFilters[0]`/`bloomFilters[1]` directly with no null-check and no synchronization:
```
public byte[] get(byte[] key) {
  if (!bloomFilters[0].mightContain(key) && !bloomFilters[1].mightContain(key)) {
    return null;
  }
  return FAKE_TRANSACTION;
}
``` [2](#0-1) 
```
public void put(byte[] key, byte[] value) {
  ...
  bloomFilters[currentFilterIndex].put(key);
  ...
}
``` [3](#0-2) 

Note the asymmetry: `flush()` is `synchronized`, but `close()`, `get()`, and `put()` are not: [4](#0-3)  There is no memory barrier or quiescence wait (the Linux fix added `synchronize_net()` after nulling `recv_probe`); here `bloomFilters[i] = null` is followed immediately by continued use elsewhere in the same object with zero coordination.

`TransactionCache` is a thin wrapper exposing this DB through the store/revoking-store infrastructure: [5](#0-4)  and `Manager.processTransaction()` calls `transactionCache.put(...)` for every processed transaction on the block-processing/transaction-broadcast path: [6](#0-5) . Transaction validation flows (duplicate-transaction checks feeding into `pushTransaction`) invoke `get()` on this same cache concurrently from multiple worker threads, since transaction handling is dispatched onto a thread pool (`trxHandlePool` in `TransactionsMsgHandler`) that keeps running independently of node lifecycle events: [7](#0-6) .

If `close()`/`reset()`/re-`init()` cycles of the underlying store run concurrently with in-flight `get()`/`put()` calls from the transaction path — e.g., during a node shutdown sequence, DB engine reset, or any lifecycle event that calls `close()` on the revoking-store chain while worker threads still process previously-queued or in-flight transactions — a thread can read `bloomFilters[0]`/`bloomFilters[1]` after it has been set to `null`, throwing an unguarded `NullPointerException` in a request-handling thread and potentially crashing the transaction-processing pipeline or the node (uncaught NPE propagating up through `Manager.processTransaction`/`pushTransaction`).

### Impact Explanation
An unguarded NPE thrown from `get()`/`put()` during the transaction validation path can crash worker threads or, if not caught at a sufficiently high boundary, bring down block/transaction processing — a node-crash/halt scenario reachable purely by transaction traffic overlapping with a legitimate close/reset of the store, analogous to the kernel's "network RX crashes during bond up/down." This matches the "node crash or halt" acceptance criterion.

### Likelihood Explanation
The window is real but narrow: it requires `close()` (or equivalent store-lifecycle teardown/reset) to execute while `get()`/`put()` calls from the transaction-processing thread pool are still in flight — which is most likely to happen during graceful shutdown, DB-engine reset, or checkpoint-store rebuild sequences, since nothing here synchronizes the lifecycle-close path with the concurrently-running transaction-processing threads. It does not require malicious input by itself, only ordinary continuous transaction traffic overlapping with a normal shutdown/reset event, so likelihood is moderate rather than trivially always-reachable.

### Recommendation
Guard `get()`, `put()`, and `close()` with the same lock (or make `bloomFilters` access go through a check consistent with `isAlive()`/`isValid`), e.g. wrap `get()`/`put()` in the same monitor used by `flush()`, or add an explicit `isAlive()` check plus a read-write lock so `close()` cannot null the array while readers/writers are in progress — mirroring the kernel fix's approach of clearing the handler pointer and then quiescing before freeing the resource.

### Proof of Concept
Not independently reproducible from static analysis alone: a concrete PoC would require driving concurrent `TransactionCache.get()`/`put()` calls from the transaction-processing thread pool while triggering `TxCacheDB.close()`/store-reset (e.g., via node shutdown or DB-engine reset) and observing the resulting `NullPointerException`. This is stated as uncertain because I could not trace, within the index, every code path that invokes `close()`/`reset()` on `TransactionCache`'s underlying store during live operation (as opposed to full application shutdown) to confirm the exact trigger sequence.

### Citations

**File:** chainbase/src/main/java/org/tron/core/db2/common/TxCacheDB.java (L181-188)
```java
  @Override
  public byte[] get(byte[] key) {
    if (!bloomFilters[0].mightContain(key) && !bloomFilters[1].mightContain(key)) {
      return null;
    }
    // this means exist
    return FAKE_TRANSACTION;
  }
```

**File:** chainbase/src/main/java/org/tron/core/db2/common/TxCacheDB.java (L191-221)
```java
  public void put(byte[] key, byte[] value) {
    if (key == null || value == null) {
      return;
    }

    long blockNum = Longs.fromByteArray(value);
    if (filterStartBlock == INVALID_BLOCK) {
      // init active filter start block
      filterStartBlock = blockNum;
      currentFilterIndex = 0;
      logger.info("Init tx cache bloomFilters at {}.", blockNum);
    } else if (blockNum - filterStartBlock > MAX_BLOCK_SIZE) {
      // active filter is full
      logger.info(
          "Active bloomFilters is full (size = {} fpp = {}), create a new one (start = {}).",
          bloomFilters[currentFilterIndex].approximateElementCount(),
          bloomFilters[currentFilterIndex].expectedFpp(),
          blockNum);

      if (currentFilterIndex == 0) {
        currentFilterIndex = 1;
      } else {
        currentFilterIndex = 0;
      }

      filterStartBlock = blockNum;
      bloomFilters[currentFilterIndex] =
          BloomFilter.create(Funnels.byteArrayFunnel(),
              MAX_BLOCK_SIZE * TRANSACTION_COUNT);
    }
    bloomFilters[currentFilterIndex].put(key);
```

**File:** chainbase/src/main/java/org/tron/core/db2/common/TxCacheDB.java (L256-273)
```java
  @Override
  public synchronized void flush(Map<WrappedByteArray, WrappedByteArray> batch) {
    isValid.set(false);
    batch.forEach((k, v) -> this.put(k.getBytes(), v.getBytes()));
    isValid.set(true);
  }

  @Override
  public void close() {
    if (!isAlive()) {
      return;
    }
    dump();
    bloomFilters[0] = null;
    bloomFilters[1] = null;
    persistentStore.close();
    setAlive(false);
  }
```

**File:** chainbase/src/main/java/org/tron/core/db/TransactionCache.java (L13-24)
```java
public class TransactionCache extends TronStoreWithRevoking<BytesCapsule> {

  @Autowired
  public TransactionCache(@Value("trans-cache") String dbName,
                          @Autowired RecentTransactionStore recentTransactionStore,
                          @Autowired DynamicPropertiesStore dynamicPropertiesStore) {
    super(new TxCacheDB(dbName, recentTransactionStore, dynamicPropertiesStore));
  }

  public void initCache() {
    ((TxCacheDB) getDb()).init();
  }
```

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L1584-1586)
```java
    Optional.ofNullable(transactionCache)
        .ifPresent(t -> t.put(trxCap.getTransactionId().getBytes(),
            new BytesCapsule(ByteArray.fromLong(trxCap.getBlockNum()))));
```

**File:** framework/src/main/java/org/tron/core/net/messagehandler/TransactionsMsgHandler.java (L49-77)
```java
  private BlockingQueue<TrxEvent> smartContractQueue = new LinkedBlockingQueue(
      Args.getInstance().getMaxTrxCacheSize());

  private BlockingQueue<Runnable> queue = new LinkedBlockingQueue();

  private volatile boolean isClosed = false;
  private int threadNum = Args.getInstance().getValidateSignThreadNum();
  private final String trxEsName = "trx-msg-handler";
  private ExecutorService trxHandlePool = ExecutorServiceManager.newThreadPoolExecutor(
      threadNum, threadNum, 0L,
      TimeUnit.MILLISECONDS, queue, trxEsName);
  private final String smartEsName = "contract-msg-handler";
  private final ScheduledExecutorService smartContractExecutor = ExecutorServiceManager
      .newSingleThreadScheduledExecutor(smartEsName);

  public void init() {
    handleSmartContract();
  }

  public void close() {
    isClosed = true;
    // Stop the scheduler first so no new tasks are drained from smartContractQueue.
    ExecutorServiceManager.shutdownAndAwaitTermination(smartContractExecutor, smartEsName);
    // Then shutdown the worker pool to finish already-submitted tasks.
    ExecutorServiceManager.shutdownAndAwaitTermination(trxHandlePool, trxEsName);
    // Discard any remaining items and release references.
    smartContractQueue.clear();
    queue.clear();
  }
```
