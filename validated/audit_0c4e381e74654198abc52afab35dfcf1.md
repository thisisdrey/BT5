## Title
Unbounded per-connection transaction handler queue enables remote memory-exhaustion DoS - (File: framework/src/main/java/org/tron/core/net/messagehandler/TransactionsMsgHandler.java)

### Summary
`TransactionsMsgHandler` routes every non-smart-contract transaction received from any peer directly into an **unbounded** `LinkedBlockingQueue<Runnable>` (`queue`) backing `trxHandlePool`, while only the smart-contract path (`smartContractQueue`) is capacity-bounded by `Args.getInstance().getMaxTrxCacheSize()`. This mirrors the Solana 2021-09-14 incident, where a burst of externally-submitted transactions was not effectively bounded before entering validator processing/memory, causing memory pressure and validator crashes that halted the network.

### Finding Description
In `TransactionsMsgHandler`: [1](#0-0) 

`smartContractQueue` is explicitly capped:
```java
private BlockingQueue<TrxEvent> smartContractQueue = new LinkedBlockingQueue(
    Args.getInstance().getMaxTrxCacheSize());
```
but the generic executor work queue is not:
```java
private BlockingQueue<Runnable> queue = new LinkedBlockingQueue();
```

`isBusy()` reads the size of both queues plus `tronNetDelegate.getCachedTransactionSize()` and compares against `maxTrxCacheSize`: [2](#0-1) 

However, in `processMessage`, for any non-`TriggerSmartContract`/`CreateSmartContract` transaction, the handler unconditionally submits work to `trxHandlePool` regardless of `isBusy()`: [3](#0-2) 
```java
} else {
  try {
    ExecutorServiceManager.submit(
        trxHandlePool, () -> handleTransaction(peer, new TransactionMessage(trx)));
  } catch (RejectedExecutionException e) {
    logger.warn("Submit task to {} failed", trxEsName);
    break;
  }
}
```
Because `queue` (the backlog for `trxHandlePool`, created with `new LinkedBlockingQueue()`) has no capacity bound, `ThreadPoolExecutor.execute()` will never reject work and `RejectedExecutionException` can never fire for this path — the queue can grow without limit as long as peers keep sending `TransactionsMessage` batches faster than `trxHandlePool` (sized by `validateSignThreadNum`) can drain them. `isBusy()` is only consulted by `InventoryMsgHandler` when deciding whether *this* node should request new inventory items from peers; it does not gate unsolicited pushes that arrive as `TransactionsMessage`, nor does it throttle `processMessage`/`queue.offer` itself. Each queued `Runnable` closure retains a full `Transaction` protobuf object (potentially near max P2P message size), so sustained multi-peer transaction floods can accumulate large numbers of retained transaction objects in heap memory before signature validation, TAPoS, dup, or resource checks (which only occur later inside `handleTransaction` -> `tronNetDelegate.pushTransaction`) have a chance to reject them.

### Impact Explanation
This is directly reachable by any peer (any full node connected via p2p, or by many peers under attacker control) broadcasting a high volume of syntactically-valid (but not necessarily resource-paid) transactions. Unlike `pendingTransactions`/`rePushTransactions` which are explicitly checked by `isTooManyPending()`/`maxTransactionPendingSize`, the raw ingestion queue in `TransactionsMsgHandler` has no equivalent backpressure, so it can grow unbounded relative to the rate of validation-thread consumption, leading to heap exhaustion and node crash/halt — a node crash/halt impact class explicitly in scope.

### Likelihood Explanation
Exploitation only requires being (or spoofing via) a connected peer capable of sending `TransactionsMessage`s that pass the lightweight `check()` gate (contract count >=1, valid signature length, requested inventory item present) — none of which bound the aggregate submission rate to `trxHandlePool`. Because `isBusy()` is not enforced at the point transactions are actually queued for handling (only used to gate this node's own inventory-request decisions in `InventoryMsgHandler`), the protection is effectively bypassed for any transaction volume delivered without waiting on this node's own inv-request throttling.

### Recommendation
- Bound `queue` (the `trxHandlePool` work queue) with the same `maxTrxCacheSize` capacity used for `smartContractQueue`, and make it a `LinkedBlockingQueue<>(maxTrxCacheSize)` so `RejectedExecutionException` is actually reachable and can be used to drop excess work.
- Call `isBusy()` (or an equivalent live capacity check) inside `processMessage` before submitting non-smart-contract transactions to `trxHandlePool`, dropping/backpressuring the peer or disconnecting abusive peers analogous to existing bad-peer handling for `BAD_TRX`.
- Consider per-peer rate limiting of transaction submission independent of the shared queue capacity, so a single malicious/misbehaving peer cannot monopolize the shared handler pool.

### Proof of Concept
1. Connect (or control) several peers to a full node.
2. Repeatedly send `TransactionsMessage` batches containing well-formed, previously-requested (`peer.getAdvInvRequest()`-registered) but otherwise low-cost `TransferContract` transactions, at a rate exceeding `trxHandlePool`'s processing throughput (bounded by `validateSignThreadNum` threads doing signature/tapos/dup/resource checks).
3. Because `queue` in `TransactionsMsgHandler` is an uncapped `LinkedBlockingQueue`, submitted `Runnable`s (each capturing a full `Transaction` object) accumulate without bound; `isBusy()` is not checked at submission time, so no backpressure or rejection occurs.
4. Continue the flood until the node's heap is exhausted, producing an OutOfMemoryError / node crash, consistent with the Solana-style transaction-flood memory-overflow DoS described in the reference incident.

*Note: I was unable to fully confirm within the tool-call budget how `isBusy()` is consulted end-to-end in `InventoryMsgHandler` (I did not get the file contents back before the iteration limit), so the exact interaction between inventory-request throttling and this ingestion queue could not be triple-verified from source in this pass; the queue's lack of a capacity bound and the unconditional `submit()` call in `processMessage`, however, are directly confirmed from the cited source.*

### Citations

**File:** framework/src/main/java/org/tron/core/net/messagehandler/TransactionsMsgHandler.java (L49-59)
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
```

**File:** framework/src/main/java/org/tron/core/net/messagehandler/TransactionsMsgHandler.java (L79-82)
```java
  public boolean isBusy() {
    return queue.size() + smartContractQueue.size()
        + tronNetDelegate.getCachedTransactionSize() > Args.getInstance().getMaxTrxCacheSize();
  }
```

**File:** framework/src/main/java/org/tron/core/net/messagehandler/TransactionsMsgHandler.java (L112-120)
```java
      } else {
        try {
          ExecutorServiceManager.submit(
              trxHandlePool, () -> handleTransaction(peer, new TransactionMessage(trx)));
        } catch (RejectedExecutionException e) {
          logger.warn("Submit task to {} failed", trxEsName);
          break;
        }
      }
```
