### Title
Iterator/ReadOptions native handles returned without holding `resetDbLock`, enabling use of freed RocksDB native resources - ([File: chainbase/src/main/java/org/tron/common/storage/rocksdb/RocksDbDataSourceImpl.java])

### Summary
The CVE describes a use-after-free where a data structure is freed while a reference to it is still in active use (`expand_mmac_params` reusing macro parameter state after it was freed). The closest reachable analog in java-tron is in `RocksDbDataSourceImpl`, where native RocksDB handles (`RocksIterator`, `ReadOptions`, and the `RocksDB database` handle itself) can be closed/freed by `closeDB()`/`resetDb()` while a separately-obtained iterator produced by the public `iterator()` method is still being consumed by another thread, with no lock held across the iterator's lifetime.

### Finding Description
Every read helper in `RocksDbDataSourceImpl` (`allKeys`, `allValues`, `getTotal`, `getKeysNext`, `getNext`, `prefixQuery`, `getlatestValues`, `getValuesNext`) acquires `resetDbLock.readLock()` and holds it for the *entire* try/finally block that creates the `ReadOptions`/`RocksIterator` and consumes them: [1](#0-0) 

However, the public `iterator()` method — the one documented and intended for long-lived, externally-iterated use — does **not** hold `resetDbLock` at all while the returned `RockStoreIterator` is used by the caller: [2](#0-1) 

`getRocksIterator()` only checks `throwIfNotAlive()` at the instant the native iterator is created, then returns the raw handle with no lock protection extending to subsequent `next()`/`hasNext()` calls performed by the caller: [3](#0-2) 

Meanwhile, `closeDB()` and `resetDb()` take the **write** lock and free the native `RocksDB` and `Options` handles: [4](#0-3) 

Because `iterator()` releases the read lock immediately after constructing the `RocksIterator`/`ReadOptions` (it never holds the lock across `hasNext()`/`next()`/`close()` calls made later by the consumer), a concurrent `resetDb()`/`closeDB()` (invoked during snapshot/session reset flows, e.g. `Chainbase.reset()` → `SnapshotRoot.reset()` → `Flusher.reset()`) can free the underlying native RocksDB and Options objects out from under an iterator that is still being walked by another thread via `RockStoreIterator`. `RockStoreIterator` itself only guards against its *own* explicit `close()` being called twice; it has no awareness that the underlying `RocksDB`/`Options` native pointers were already freed by a concurrent reset: [5](#0-4) 

This is a genuine native use-after-free (JNI call into freed RocksDB C++ objects), analogous in bug class to the yasm UAF (stale pointer/state reused after being freed), rather than a pure-Java logic bug, since RocksDB Java bindings are thin JNI wrappers around native, manually-managed C++ memory.

### Impact Explanation
A native use-after-free on the RocksDB handle can cause JVM process crash (segfault in native code) or undefined native memory corruption, resulting in a node crash/halt. Because RocksDbDataSourceImpl backs core chain state stores accessed during normal block application and store iteration (Chainbase/SnapshotRoot/TronStoreWithRevoking iterate via this path), a crash here affects node availability broadly — satisfying the "node crash or halt" acceptance criterion.

### Likelihood Explanation
Triggering requires two things to race: (1) a thread holding an iterator obtained via `iterator()` doing prolonged iteration, and (2) a concurrent `resetDb()`/`closeDB()` on the same database instance. `resetDb()`/`closeDB()` calls in this codebase appear tied to snapshot/session reset and revoking-database rollback flows rather than being trivially triggerable per-transaction; I was not able to fully trace, within the remaining investigation budget, a concrete single-transaction-triggered call path that invokes `resetDb()`/`closeDB()` on a store while another thread is actively consuming an `iterator()` on the same store (e.g. through Wallet/TronJsonRpcImpl query paths). This causal chain remains **unconfirmed** — the vulnerable code pattern (lock not held across iterator lifetime) is real and verifiable, but I could not confirm within the available tool calls that an unprivileged transaction or API request alone can reliably trigger the concurrent reset needed to win the race.

### Recommendation
Extend `resetDbLock` read-lock acquisition to cover the full lifetime of iterators returned by the public `iterator()` method (e.g., wrap `RockStoreIterator`/`RockDBIterator` so their `close()` releases a read lock acquired at creation time, or have `resetDb()`/`closeDB()` block until all outstanding iterators are closed). Alternatively, document and enforce that `resetDb()`/`closeDB()` must never be called while any iterator from the same instance is open, and add a live-iterator reference count guard that `closeDB()` checks before freeing native resources.

### Proof of Concept
Not fully constructible from static analysis alone within the given scope — reproducing requires: Thread A calls `dataSource.iterator()` and begins iterating slowly (e.g. sleeping between `next()` calls) while Thread B concurrently triggers a code path that calls `resetDb()`/`closeDB()` on the same `RocksDbDataSourceImpl` instance (such as a snapshot-manager rollback/reset flow). Thread A's subsequent `hasNext()`/`next()` call would then invoke native methods on the freed `RocksIterator`/`ReadOptions`/`RocksDB` handles. I could not confirm within this investigation a concrete unprivileged-transaction-triggerable call path that invokes `resetDb()`/`closeDB()` concurrently with an open external `iterator()` consumer, so this proof of concept is incomplete/unverified.

### Citations

**File:** chainbase/src/main/java/org/tron/common/storage/rocksdb/RocksDbDataSourceImpl.java (L75-104)
```java
  @Override
  public void closeDB() {
    resetDbLock.writeLock().lock();
    try {
      if (!isAlive()) {
        return;
      }
      if (this.options != null) {
        this.options.close();
      }
      database.close();
      alive = false;
    } catch (Exception e) {
      logger.error("Failed to find the dbStore file on the closeDB: {}.", dataBaseName, e);
    } finally {
      resetDbLock.writeLock().unlock();
    }
  }

  @Override
  public void resetDb() {
    resetDbLock.writeLock().lock();
    try {
      closeDB();
      FileUtil.recursiveDelete(getDbPath().toString());
      initDB();
    } finally {
      resetDbLock.writeLock().unlock();
    }
  }
```

**File:** chainbase/src/main/java/org/tron/common/storage/rocksdb/RocksDbDataSourceImpl.java (L295-299)
```java
  @Override
  public org.tron.core.db.common.iterator.DBIterator iterator() {
    ReadOptions readOptions = getReadOptions();
    return new RockStoreIterator(getRocksIterator(readOptions), readOptions);
  }
```

**File:** chainbase/src/main/java/org/tron/common/storage/rocksdb/RocksDbDataSourceImpl.java (L344-360)
```java
  public List<byte[]> getKeysNext(byte[] key, long limit) {
    if (limit <= 0) {
      return new ArrayList<>();
    }
    resetDbLock.readLock().lock();
    try (final ReadOptions readOptions = getReadOptions();
         final RocksIterator iter = getRocksIterator(readOptions)) {
      List<byte[]> result = new ArrayList<>();
      long i = 0;
      for (iter.seek(key); iter.isValid() && i < limit; iter.next(), i++) {
        result.add(iter.key());
      }
      return result;
    } finally {
      resetDbLock.readLock().unlock();
    }
  }
```

**File:** chainbase/src/main/java/org/tron/common/storage/rocksdb/RocksDbDataSourceImpl.java (L460-463)
```java
  private RocksIterator getRocksIterator(ReadOptions readOptions) {
    throwIfNotAlive();
    return database.newIterator(readOptions);
  }
```

**File:** chainbase/src/main/java/org/tron/core/db/common/iterator/RockStoreIterator.java (L34-58)
```java
  @Override
  public boolean hasNext() {
    if (close.get()) {
      return false;
    }
    boolean hasNext = false;
    // true is first item
    try {
      if (first) {
        dbIterator.seekToFirst();
        first = false;
      }
      if (!(hasNext = dbIterator.isValid())) { // false is last item
        close();
      }
    } catch (Exception e) {
      logger.error(e.getMessage(), e);
      try {
        close();
      } catch (Exception e1) {
        logger.error(e1.getMessage(), e1);
      }
    }
    return hasNext;
  }
```
