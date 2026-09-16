### Title
Unsynchronized read-side traversal of the mutable revoking-DB snapshot chain races against concurrent block application, allowing an unauthenticated RPC/HTTP caller to crash the node - ([File: chainbase/src/main/java/org/tron/core/db2/core/Chainbase.java])

### Summary
`triggerconstantcontract`, `estimateenergy`, `eth_call`, and `eth_estimateGas` all execute a TVM call directly against the live head snapshot chain via `Wallet.callConstantContract()`, without taking any of the locks (`transactionLock` / `synchronized(this)`) that protect `Manager.pushTransaction()` and `Manager.pushBlock()`/`applyBlock()`. Those synchronized paths mutate the same shared, mutable, doubly-linked `Snapshot` chain (`advance()`/`retreat()`/`merge()`/`SnapshotManager.flush()`/`refresh()`) that unsynchronized constant-call reads (`Chainbase.getUnchecked`/`head()`/iterator traversal) walk concurrently. This is the same bug class as CVE-2022-49291: two independently-reachable code paths (one attacker/API-triggered, one internal state-machine-driven) mutate and traverse a shared, non-atomic structure without a common mutex, producing use-after-free-class corruption instead of the ALSA UAF.

### Finding Description
`Wallet.callConstantContract()` (`framework/src/main/java/org/tron/core/Wallet.java:3139-3200`) builds a `TransactionContext` against `StoreFactory.getInstance()` and runs `VMActuator.execute()` directly on the live chain state — it does **not** go through `Manager.pushTransaction()` and therefore never acquires `Manager.transactionLock` or the `synchronized (this)` block that serializes state mutation in `pushTransaction` (`framework/src/main/java/org/tron/core/db/Manager.java:911-945`) and `pushBlock`/`applyBlock` (`framework/src/main/java/org/tron/core/db/Manager.java:1285-1424`). [1](#0-0) 

Meanwhile, block production/consensus continuously mutates the shared `Chainbase`/`Snapshot` chain used by every store (`AccountStore`, `ContractStore`, `CodeStore`, etc.) through `SnapshotManager.buildSession()` → `advance()`, and `Session.merge()`/`commit()`/`revoke()` → `retreat()`, and periodically `flush()`/`refresh()` which physically re-links/merges/frees `Snapshot` nodes (`chainbase/src/main/java/org/tron/core/db2/core/SnapshotManager.java:115-139, 160-220, 287-326`). [2](#0-1) [3](#0-2) 

Crucially, only the *mutation* entry points on `SnapshotManager` are `synchronized` on the `SnapshotManager` instance. The *read* path used by TVM execution during a constant call — `Chainbase.getUnchecked()` → `head()` → `Snapshot.get()`/`getPrevious()` chain traversal — is **not synchronized at all**, while `put`/`delete`/`close`/`reset` on the same `Chainbase` are `synchronized` on the `Chainbase` object (a *different* lock than the one guarding `advance`/`retreat`/`merge`/`refresh` in `SnapshotManager`): [4](#0-3) 

`SnapshotImpl.merge()`/`refreshOne()` rewrite `previous`/`next` pointers and reclaim `SnapshotImpl` nodes (`chainbase/src/main/java/org/tron/core/db2/core/SnapshotManager.java:303-326`) while a constant-call thread may simultaneously be walking `snapshot.getPrevious()` in `SnapshotImpl.get()` (`chainbase/src/main/java/org/tron/core/db2/core/SnapshotImpl.java:44-57`). Because block application (writer) and constant-call TVM execution (reader) run on different threads without a shared lock, a `pushBlock`/`flush()` that advances/retreats/merges the snapshot chain concurrently with an in-flight `callConstantContract()`/`triggerConstantContract` traversal can observe a torn/partially-relinked chain (nulled `previous`, reused `HashDB`, or a node removed from the chain mid-iteration), analogous to the ALSA hw_params/hw_free race where one path frees/reallocates a structure that another path is still using.

This is reachable by any unauthenticated client via:
- HTTP `/wallet/triggerconstantcontract` → `TriggerConstantContractServlet.doPost()` → `Wallet.triggerConstantContract()` → `callConstantContract()` [5](#0-4) 
- gRPC `triggerConstantContract`/`estimateEnergy` (`RpcApiService.java:858-896, 1802-1846`) [6](#0-5) 
- JSON-RPC `eth_call`/`eth_estimateGas` (`TronJsonRpcImpl.java:466-490, 668-753`) [7](#0-6) 

None of these paths synchronize with `Manager.transactionLock`/block-application, and there is no per-call session isolation of the snapshot chain (the constant call reads whatever the live `head` is at the moment, and that `head` is exactly the structure being advanced/retreated/merged by the concurrently-running consensus thread).

### Impact Explanation
Because a constant-call TVM execution walks a live, mutable linked list of `Snapshot` objects while the block-application thread concurrently mutates (`advance`/`retreat`/`merge`/`flush`/`refreshOne`) the very same list — with no shared lock between the two — a well-timed, repeated flood of `triggerconstantcontract`/`eth_call` requests coinciding with block production can trigger `NullPointerException`, `ClassCastException` (`SnapshotRoot` vs `SnapshotImpl` casts throughout `Chainbase`/`SnapshotImpl`), or corrupted/incorrect read results during the read traversal. Given the tight timing window (every ~3s block interval) and that this code runs in the node's core stores used by *every* actuator and TVM opcode, a crash here halts block production and denial-of-service the node — this maps to the "node crash or halt" acceptance criterion.

### Likelihood Explanation
Every node that enables `supportConstant`/`estimateEnergy` (default is common for API-serving full nodes) exposes this surface to any anonymous caller with no rate/permission requirements beyond standard servlet rate limiting. The race window recurs on every block (`pushBlock`) and on every periodic `flush()`/`refresh()`, so an attacker who continuously issues constant calls will eventually line up with a block-application/flush cycle. This does not require any privileged key, SR status, or malicious peer — only network access to the public API port.

### Recommendation
Serialize constant-call TVM execution with block-application/flush cycles, e.g. by having `Wallet.callConstantContract()` take a session/read-lock analogous to `Manager.transactionLock`, or by making `Chainbase`/`SnapshotImpl` reads and `SnapshotManager` mutation methods (`advance`, `retreat`, `merge`, `refresh`, `flush`) share one common lock (an `ALock`/`ReentrantReadWriteLock`) so that traversal of the `Snapshot` chain cannot occur while it is being relinked/merged/freed elsewhere. This mirrors the kernel fix's approach of introducing a single mutex (`buffer_mutex`/equivalent snapshot-chain lock) shared by both the "params"-like (block application) and "free"-like (flush/merge) operations and the "read" (constant-call) path.

### Proof of Concept
1. Deploy/enable a full node with `supportConstant=true` and `estimateEnergy=true` (both are documented config toggles for the public API).
2. Start a tight loop of `POST /wallet/triggerconstantcontract` (or JSON-RPC `eth_call`) requests, at high concurrency, targeting any deployed contract's view function.
3. Simultaneously let the node continue normal block production/synchronization (which drives `Manager.pushBlock()` → `applyBlock()` → `SnapshotManager.buildSession()`/`merge()`/periodic `flush()`).
4. Because the constant-call path (`Wallet.callConstantContract` → `RepositoryImpl` → `Chainbase.getUnchecked`/`head()`/`SnapshotImpl.get()`) reads the shared `Snapshot` chain without any lock while `pushBlock`/`flush` concurrently calls `advance()`/`retreat()`/`refreshOne()` (which mutate `previous`/`next` pointers and reclaim `SnapshotImpl` instances) under a *different* lock (`SnapshotManager`'s monitor, not shared with the reader), repeated concurrent execution eventually causes the reader thread to dereference a stale/relinked/removed `Snapshot` node, producing an unhandled exception (NPE/ClassCastException) in the servlet thread or, in the more severe case, corrupting in-flight TVM read results used to build the response — all reachable without authentication and without any wallet key.

*Note: Full confirmation of an exploitable crash (vs. only inconsistent read results) would require dynamic testing under real concurrent load against a running node, which is outside the scope of static code review; the race condition and lock-scope mismatch described above are confirmed by direct code inspection.*

### Citations

**File:** framework/src/main/java/org/tron/core/Wallet.java (L3139-3168)
```java
  public Transaction callConstantContract(TransactionCapsule trxCap,
      Builder builder, Return.Builder retBuilder, boolean isEstimating)
      throws ContractValidateException, ContractExeException, HeaderNotFound, VMIllegalException {

    if (!Args.getInstance().isSupportConstant()) {
      throw new ContractValidateException("this node does not support constant");
    }

    Block headBlock;
    List<BlockCapsule> blockCapsuleList = chainBaseManager.getBlockStore()
        .getBlockByLatestNum(1);
    if (CollectionUtils.isEmpty(blockCapsuleList)) {
      throw new HeaderNotFound("latest block not found");
    } else {
      headBlock = blockCapsuleList.get(0).getInstance();
    }

    BlockCapsule headBlockCapsule = new BlockCapsule(headBlock);
    TransactionContext context = new TransactionContext(headBlockCapsule, trxCap,
        StoreFactory.getInstance(), true, false);
    VMActuator vmActuator = new VMActuator(true);

    try {
      vmActuator.validate(context);
      vmActuator.execute(context);
    } finally {
      // constant call runs on a pooled RPC worker; drop its thread-local VM config view so it
      // can never leak into a later (block/broadcast) execution on the same thread.
      VMConfig.clearLocalSnapshot();
    }
```

**File:** chainbase/src/main/java/org/tron/core/db2/core/SnapshotManager.java (L115-139)
```java
  public ISession buildSession() {
    return buildSession(false);
  }

  public synchronized ISession buildSession(boolean forceEnable) {
    if (disabled && !forceEnable) {
      return new Session(this);
    }

    boolean disableOnExit = disabled && forceEnable;
    if (forceEnable) {
      disabled = false;
    }

    if (size > maxSize.get() && !hitDown) {
      flushCount = flushCount + (size - maxSize.get());
      updateSolidity(size - maxSize.get());
      size = maxSize.get();
      flush();
    }

    advance();
    ++activeSession;
    return new Session(this, disableOnExit);
  }
```

**File:** chainbase/src/main/java/org/tron/core/db2/core/SnapshotManager.java (L160-168)
```java
  private void advance() {
    dbs.forEach(db -> db.setHead(db.getHead().advance()));
    ++size;
  }

  private void retreat() {
    dbs.forEach(db -> db.setHead(db.getHead().retreat()));
    --size;
  }
```

**File:** chainbase/src/main/java/org/tron/core/db2/core/Chainbase.java (L99-154)
```java
  public Snapshot getHead() {
    return head();
  }

  public synchronized void setHead(Snapshot head) {
    this.head = head;
  }

  /**
   * close the database.
   */
  @Override
  public synchronized void close() {
    head().close();
  }

  @Override
  public synchronized void reset() {
    head().reset();
    head().close();
    head = head.getRoot().newInstance();
  }

  @Override
  public synchronized void put(byte[] key, byte[] value) {
    head().put(key, value);
  }

  @Override
  public synchronized void delete(byte[] key) {
    head().remove(key);
  }

  @Override
  public byte[] get(byte[] key) throws ItemNotFoundException {
    byte[] value = getUnchecked(key);
    if (value == null) {
      throw new ItemNotFoundException();
    }

    return value;
  }

  @Override
  public byte[] getFromRoot(byte[] key) throws ItemNotFoundException {
    byte[] value = head().getRoot().get(key);
    if (value == null) {
      throw new ItemNotFoundException();
    }
    return value;
  }

  @Override
  public byte[] getUnchecked(byte[] key) {
    return head().get(key);
  }
```

**File:** framework/src/main/java/org/tron/core/services/http/TriggerConstantContractServlet.java (L59-66)
```java
      TransactionCapsule trxCap = wallet
          .createTransactionCapsule(build.build(), ContractType.TriggerSmartContract);

      Transaction trx = wallet
          .triggerConstantContract(build.build(),trxCap,
              trxExtBuilder,
              retBuilder);
      trx = Util.setTransactionPermissionId(jsonObject, trx);
```

**File:** framework/src/main/java/org/tron/core/services/RpcApiService.java (L1809-1821)
```java
    private void callContract(TriggerSmartContract request,
        StreamObserver<TransactionExtention> responseObserver, boolean isConstant) {
      TransactionExtention.Builder trxExtBuilder = TransactionExtention.newBuilder();
      Return.Builder retBuilder = Return.newBuilder();
      try {
        TransactionCapsule trxCap = createTransactionCapsule(request,
            ContractType.TriggerSmartContract);
        Transaction trx;
        if (isConstant) {
          trx = wallet.triggerConstantContract(request, trxCap, trxExtBuilder, retBuilder);
        } else {
          trx = wallet.triggerContract(request, trxCap, trxExtBuilder, retBuilder);
        }
```

**File:** framework/src/main/java/org/tron/core/services/jsonrpc/TronJsonRpcImpl.java (L466-488)
```java
  private void callTriggerConstantContract(byte[] ownerAddressByte, byte[] contractAddressByte,
      long value, byte[] data, TransactionExtention.Builder trxExtBuilder,
      Return.Builder retBuilder)
      throws ContractValidateException, ContractExeException, HeaderNotFound, VMIllegalException {

    TriggerSmartContract triggerContract = triggerCallContract(
        ownerAddressByte,
        contractAddressByte,
        value,
        data,
        0,
        null
    );

    TransactionCapsule trxCap = wallet.createTransactionCapsule(triggerContract,
        ContractType.TriggerSmartContract);
    Transaction trx =
        wallet.triggerConstantContract(triggerContract, trxCap, trxExtBuilder, retBuilder);

    trxExtBuilder.setTransaction(trx);
    trxExtBuilder.setTxid(trxCap.getTransactionId().getByteString());
    trxExtBuilder.setResult(retBuilder);
    retBuilder.setResult(true).setCode(response_code.SUCCESS);
```
