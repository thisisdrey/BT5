### Title
Broadcast-triggered transaction execution serializes on the same monitor as block application, enabling API/consensus blockage - ([File: framework/src/main/java/org/tron/core/db/Manager.java])

### Summary
CVE-2019-20485 describes libvirt holding a per-domain job/monitor lock for the full duration of a guest-agent query, so any other API call needing that same lock blocks until the (potentially slow) query finishes. `Manager.pushTransaction()` in java-tron has the same structural flaw: it takes the Manager's own intrinsic monitor (`synchronized (this)`) and, while holding it, runs the entire transaction-execution pipeline (`processTransaction()` → `TransactionTrace.exec()` → TVM execution for smart-contract calls/deployments). `Manager.pushBlock()` synchronizes on the exact same monitor (`synchronized (this)`), so any thread applying a new block must wait for whichever broadcast transaction currently holds the lock to finish executing.

### Finding Description
`pushTransaction()` first waits out `isBlockWaitingLock()` under `synchronized (transactionLock)`, then enters `synchronized (this)` and calls `processTransaction(trx, null)` inside that block: [1](#0-0) 

`processTransaction()` performs full contract validation and execution, including `trace.exec()` which drives TVM execution for `TriggerSmartContract`/`CreateSmartContract` payloads: [2](#0-1) 

`pushBlock()`, which is invoked from the block-application path (`TronNetDelegate.processBlock` → `Manager.pushBlock`), also synchronizes on `this` — the same monitor: [3](#0-2) 

Because both the broadcast-transaction path and the block-application path share the identical object monitor, any unprivileged client that submits smart-contract transactions (deploy or call) causes the node's single Manager monitor to be occupied for the duration of TVM execution for every broadcast transaction accepted into the queue. While that monitor is held, `pushBlock()` cannot proceed, so newly received blocks (from consensus/sync) cannot be applied until the transaction finishes — the same "hold an exclusive job/monitor lock across a query" mishandling pattern as CVE-2019-20485, just with TVM execution standing in for the guest-agent query and the Manager intrinsic lock standing in for libvirt's domain job lock.

### Impact Explanation
An attacker who repeatedly broadcasts transactions invoking contracts that consume close to the per-transaction CPU/energy limit can keep the Manager monitor almost continuously occupied by `pushTransaction()`. Since `pushBlock()` requires the same monitor, block application is delayed, degrading the node's ability to keep up with the chain (an "API the node can no longer serve" / node halt-adjacent condition), which is squarely within the accepted impact categories (node crash or halt, chain split risk from falling behind).

### Likelihood Explanation
Reaching this path requires only submitting ordinary, unprivileged broadcast transactions (`BroadcastTransaction` HTTP/gRPC entry points feeding `Wallet`/`Manager.pushTransaction`) that call or deploy smart contracts — no special permissions, signatures beyond the sender's own key, or witness/SR status are needed. The condition is easy to trigger repeatedly and cheaply relative to the disruption caused, since many broadcaster threads/clients can queue transactions concurrently while each one holds the shared monitor during its own TVM execution.

### Recommendation
Avoid sharing a single coarse-grained monitor between the broadcast-transaction execution path and block application. Options: use separate locks for "apply pending transaction" vs "apply new block", shrink the critical section in `pushTransaction()` so TVM execution happens outside the lock and only the pending-list mutation and session merge happen under the lock, or give block application priority (e.g., have `pushTransaction()` re-check `isBlockWaitingLock()` inside the synchronized section and back off before long-running execution rather than only before acquiring the lock).

### Proof of Concept
1. Deploy a contract whose invoked function performs computation near the configured `maxCpuTimeOfOneTx` limit (but under it, so it doesn't abort).
2. From an unprivileged account, repeatedly broadcast `TriggerSmartContract` transactions invoking that function via `/wallet/broadcasttransaction` or the gRPC `BroadcastTransaction` API — each call reaches `Manager.pushTransaction()` and holds `synchronized (this)` for the duration of `processTransaction()`/TVM execution.
3. Concurrently, have the node receive/relay a new block through `TronNetDelegate.processBlock()` → `Manager.pushBlock()`, which blocks on the same monitor.
4. Observe that block application latency grows proportionally to the queued/broadcast transaction execution time, demonstrating the shared-lock-based API/consensus blockage.

### Citations

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L911-940)
```java
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
```

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L1283-1301)
```java
    setBlockWaitLock(true);
    try {
      synchronized (this) {
        Metrics.histogramObserve(blockedTimer.get());
        blockedTimer.remove();
        if (Metrics.enabled()) {
          Metrics.histogramObserve(MetricKeys.Histogram.BLOCK_TRANSACTION_COUNT,
              block.getTransactions().size(),
              StringUtil.encode58Check(block.getWitnessAddress().toByteArray()));
        }
        long headerNumber = getDynamicPropertiesStore().getLatestBlockHeaderNumber();
        if (block.getNum() <= headerNumber && khaosDb.containBlockInMiniStore(block.getBlockId())) {
          logger.info("Block {} is already exist.", block.getBlockId().getString());
          return;
        }
        final Histogram.Timer timer = Metrics.histogramStartTimer(
                MetricKeys.Histogram.BLOCK_PUSH_LATENCY);
        long start = System.currentTimeMillis();
        List<TransactionCapsule> txs = getVerifyTxs(block);
```

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L1551-1582)
```java
    TransactionTrace trace = new TransactionTrace(trxCap, StoreFactory.getInstance(),
        new RuntimeImpl());
    trxCap.setTrxTrace(trace);

    consumeBandwidth(trxCap, trace);
    consumeMultiSignFee(trxCap, trace);
    consumeMemoFee(trxCap, trace);

    trace.init(blockCap, eventPluginLoaded);
    trace.checkIsConstant();
    trace.exec();

    if (Objects.nonNull(blockCap)) {
      trace.setResult();
      if (trace.checkNeedRetry()) {
        trace.init(blockCap, eventPluginLoaded);
        trace.checkIsConstant();
        trace.exec();
        trace.setResult();
        logger.info("Retry result when push: {}, for tx id: {}, tx resultCode in receipt: {}.",
            blockCap.hasWitnessSignature(), txId, trace.getReceipt().getResult());
      }
      if (blockCap.hasWitnessSignature()) {
        trace.check();
      }
    }

    trace.finalization();
    if (getDynamicPropertiesStore().supportVM()) {
      trxCap.setResult(trace.getTransactionContext());
    }
    chainBaseManager.getTransactionStore().put(trxCap.getTransactionId().getBytes(), trxCap);
```
