## Title
Unsynchronized reads of `Chainbase.head` racing with background snapshot refresh in `SnapshotManager` - (File: `chainbase/src/main/java/org/tron/core/db2/core/Chainbase.java`)

### Summary
This is the same bug class as the Nethermind `FullPruningDb` fix: a mutable reference that a background maintenance operation reassigns is read by hot-path query methods without the synchronization/locking that the writer uses, creating a TOCTOU window where the traversal walks a snapshot chain that is concurrently being rewired.

### Finding Description
`Chainbase` keeps the current snapshot pointer in a plain (non-`volatile`) field `head` and exposes it through the private `head()` accessor, used by essentially every read path: `get`/`getUnchecked`/`has`, `iterator`, `getValuesNext`, `getKeysNext`, `getlatestValues`, `getNext`, `prefixQuery`. [1](#0-0) [2](#0-1) [3](#0-2) 

`setHead()` is the only writer and is marked `synchronized`, but none of the read methods above take that lock — `head()` itself is not `synchronized` and is called unguarded from `get`, `getUnchecked`, `has`, `getValuesNext`, `getKeysNext`, `getNext`, and `prefixQuery`. [4](#0-3) [5](#0-4) 

`SnapshotManager.refresh()`/`refreshOne()` mutate the exact same snapshot chain (`getPrevious`, `setPrevious`, `getNext`, `setNext`, `db.setHead(root)`) from a background per-DB executor (`flushServices`), triggered on every block's `buildSession()`/`flush()` when the flush threshold is hit — i.e., during ordinary block application, not just pruning: [6](#0-5) [7](#0-6) 

While `refreshOne` is rewiring `previous`/`next` links and calling `db.setHead(root)` for a given `Chainbase`, a concurrent, unsynchronized read on that same `Chainbase` instance (`head()` → `snapshot.getPrevious()` chain walk in `SnapshotImpl.get`/`collect`) can observe a partially-updated chain — the exact "field read while a concurrent mutator resets it" race pattern flagged in the Nethermind `FullPruningDb` fix, just applied to the snapshot linked-list pointer instead of a pruning context field. [8](#0-7) 

### Impact Explanation
`Chainbase.get()`/`getUnchecked()`/`has()` back essentially all store reads used by `Wallet` and `TronJsonRpcImpl` to answer account/balance/contract queries, and by actuators during block execution. A read racing a concurrent `refreshOne` chain rewrite can throw an unexpected `NullPointerException` (denial of service on that query/RPC thread) or, more subtly, return an inconsistent/stale value from a half-relinked snapshot chain, which could surface as an incorrect balance or state read served to an external API caller.

### Likelihood Explanation
`refreshOne`/`flush` run automatically on the normal block-commit path whenever the flush-count threshold (`maxFlushCount`) is reached — this is routine node operation, not an attacker-controlled or rare maintenance event, so the race window opens continuously under regular chain traffic. Any unprivileged client issuing a read (`GetAccount`, JSON-RPC `eth_getBalance`/`eth_call`, `TriggerConstantContract`, etc.) at the moment a flush is running against the relevant store can hit the window.

### Recommendation
Make `Chainbase.head` `volatile` and/or have all read accessors take the same lock discipline as `setHead()`/`close()`/`reset()`/`put()`/`delete()` (which are already `synchronized`), so reads cannot observe a snapshot chain mid-mutation by `SnapshotManager.refreshOne`.

### Proof of Concept
1. Configure a store so `maxFlushCount`/flush thresholds trigger frequently (as in normal operation, `SnapshotManager.flush()` runs on each `buildSession()` once `flushCount >= maxFlushCount`).
2. From a separate thread, continuously issue read RPCs that resolve to `Chainbase.get/has/getUnchecked` on a store undergoing flush (e.g., repeated `GetAccount`/`eth_getBalance` calls).
3. Concurrently drive block commits so `SnapshotManager.refresh()`/`refreshOne()` executes on the background `flushServices` executor for that store, relinking `previous`/`next` and calling `db.setHead(root)`.
4. Observe intermittent `NullPointerException` or inconsistent read results on the query thread due to the unsynchronized `head()` read racing the synchronized/executor-based chain mutation — matching the same read-method race class fixed upstream in Nethermind's `FullPruningDb`. [9](#0-8)

### Citations

**File:** chainbase/src/main/java/org/tron/core/db2/core/Chainbase.java (L38-44)
```java
  private Snapshot head;

  public Chainbase(Snapshot head) {
    this.head = head;
    cursor.set(Cursor.HEAD);
    offset.set(0L);
  }
```

**File:** chainbase/src/main/java/org/tron/core/db2/core/Chainbase.java (L70-105)
```java
  private Snapshot head() {
    if (cursor.get() == null) {
      return head;
    }

    switch (cursor.get()) {
      case HEAD:
        return head;
      case SOLIDITY:
        return head.getSolidity();
      case PBFT:
        if (offset.get() == null) {
          return head.getSolidity();
        }

        if (offset.get() >= 0) {
          Snapshot tmp = head;
          for (int i = 0; i < offset.get() && tmp != tmp.getRoot(); i++) {
            tmp = tmp.getPrevious();
          }
          return tmp;
        } else {
          return head.getSolidity();
        }
      default:
        return head;
    }
  }

  public Snapshot getHead() {
    return head();
  }

  public synchronized void setHead(Snapshot head) {
    this.head = head;
  }
```

**File:** chainbase/src/main/java/org/tron/core/db2/core/Chainbase.java (L151-164)
```java
  @Override
  public byte[] getUnchecked(byte[] key) {
    return head().get(key);
  }

  @Override
  public boolean has(byte[] key) {
    return getUnchecked(key) != null;
  }

  @Override
  public synchronized Iterator<Map.Entry<byte[], byte[]>> iterator() {
    return head().iterator();
  }
```

**File:** chainbase/src/main/java/org/tron/core/db2/core/Chainbase.java (L207-210)
```java
  @Override
  public List<byte[]> getKeysNext(byte[] key, long limit) {
    return getKeysNext(head(), key, limit);
  }
```

**File:** chainbase/src/main/java/org/tron/core/db2/core/Chainbase.java (L352-368)
```java
  public Map<WrappedByteArray, byte[]> prefixQuery(byte[] key) {
    Map<WrappedByteArray, byte[]> result = prefixQueryRoot(key);
    Map<WrappedByteArray, byte[]>  snapshot = prefixQuerySnapshot(key);
    result.putAll(snapshot);
    result.entrySet().removeIf(e -> e.getValue() == null);
    return result;
  }

  private Map<WrappedByteArray, byte[]> prefixQueryRoot(byte[] key) {
    Map<WrappedByteArray, byte[]> result = new HashMap<>();
    if (((SnapshotRoot) head.getRoot()).db.getClass() == LevelDB.class) {
      result = ((LevelDB) ((SnapshotRoot) head.getRoot()).db).getDb().prefixQuery(key);
    } else if (((SnapshotRoot) head.getRoot()).db.getClass() == RocksDB.class) {
      result = ((RocksDB) ((SnapshotRoot) head.getRoot()).db).getDb().prefixQuery(key);
    }
    return result;
  }
```

**File:** chainbase/src/main/java/org/tron/core/db2/core/SnapshotManager.java (L287-326)
```java
  private void refresh() {
    List<ListenableFuture<?>> futures = new ArrayList<>(dbs.size());
    for (Chainbase db : dbs) {
      futures.add(flushServices.get(db.getDbName()).submit(() -> refreshOne(db)));
    }
    Future<?> future = Futures.allAsList(futures);
    try {
      future.get();
    } catch (InterruptedException e) {
      Thread.currentThread().interrupt();
      throw new TronDBException(e);
    } catch (ExecutionException e) {
      throw new TronDBException(e);
    }
  }

  private void refreshOne(Chainbase db) {
    if (Snapshot.isRoot(db.getHead())) {
      return;
    }

    List<Snapshot> snapshots = new ArrayList<>();

    SnapshotRoot root = (SnapshotRoot) db.getHead().getRoot();
    Snapshot next = root;
    for (int i = 0; i < flushCount; ++i) {
      next = next.getNext();
      snapshots.add(next);
    }

    root.merge(snapshots);

    root.resetSolidity();
    if (db.getHead() == next) {
      db.setHead(root);
    } else {
      next.getNext().setPrevious(root);
      root.setNext(next.getNext());
    }
  }
```

**File:** chainbase/src/main/java/org/tron/core/db2/core/SnapshotManager.java (L328-355)
```java
  public void flush() {
    if (unChecked) {
      return;
    }

    if (shouldBeRefreshed()) {
      try {
        long start = System.currentTimeMillis();
        if (!isV2Open()) {
          deleteCheckpoint();
        }
        createCheckpoint();

        long checkPointEnd = System.currentTimeMillis();
        refresh();
        flushCount = 0;
        logger.info("Flush cost: {} ms, create checkpoint cost: {} ms, refresh cost: {} ms.",
            System.currentTimeMillis() - start,
            checkPointEnd - start,
            System.currentTimeMillis() - checkPointEnd
        );
      } catch (TronDBException e) {
        logger.error(" Find fatal error, program will be exited soon.", e);
        hitDown = true;
        throw new TronError(e, TronError.ErrCode.DB_FLUSH);
      }
    }
  }
```

**File:** chainbase/src/main/java/org/tron/core/db2/core/SnapshotImpl.java (L39-57)
```java
  @Override
  public byte[] get(byte[] key) {
    return get(this, key);
  }

  private byte[] get(Snapshot head, byte[] key) {
    Snapshot snapshot = head;
    Value value;

    while (Snapshot.isImpl(snapshot)) {
      if ((value = ((SnapshotImpl) snapshot).db.get(Key.of(key))) != null) {
        return value.getBytes();
      }

      snapshot = snapshot.getPrevious();
    }

    return snapshot == null ? null : snapshot.get(key);
  }
```
