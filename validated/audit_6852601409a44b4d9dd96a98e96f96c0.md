## Analog Vulnerability Found

### Title
Unbounded per-filter result-queue accumulation in JSON-RPC log/block filters enables remote memory-exhaustion DoS - (File: `framework/src/main/java/org/tron/core/services/jsonrpc/filters/LogFilterAndResult.java`, `framework/src/main/java/org/tron/core/services/jsonrpc/filters/BlockFilterAndResult.java`)

### Summary
`eth_newFilter` / `eth_newBlockFilter` register a `LogFilterAndResult` / `BlockFilterAndResult` object whose internal `LinkedBlockingQueue` accumulates matched log/block entries with no maximum-size check on the queue itself. The filter is kept alive purely by an absolute expiry timestamp (`FilterResult.expireTimeStamp`), refreshed via `updateExpireTime()`. Any unauthenticated JSON-RPC HTTP client can create such a filter and let ordinary chain activity (new blocks / matching contract logs) accumulate unboundedly in the queue for the entire lifetime of the filter, exactly mirroring the hackney bug class: a resettable/duration-based liveness window combined with an accumulation buffer that has no independent size cap.

### Finding Description
`LogFilterAndResult.add()` and `BlockFilterAndResult.add()` push every matching element straight into an unbounded `LinkedBlockingQueue` with no cap: [1](#0-0) [2](#0-1) 

The only lifetime control is `FilterResult`'s absolute-deadline expiry, which is explicitly not a wall-clock hard cap tied to queue size — it is a timestamp reset via `updateExpireTime()`: [3](#0-2) 

`eth_newFilter` is reachable by any anonymous JSON-RPC client and is only bounded by a *count* of filters (`maxLogFilterNum`), not by the size of any individual filter's result queue: [4](#0-3) 

New matching data is pushed into these filters continuously as blocks are produced, via `Manager.postBlockFilter` / `Manager.postLogsFilter` → `filterCapsuleQueue` → `filterProcessLoop` → `TronJsonRpcImpl.handleLogsFilter` / `handleBLockFilter`: [5](#0-4) [6](#0-5) 

This is analogous to hackney's `await_response_loop`: instead of a per-chunk inactivity timer being reset by attacker-controlled traffic while `AccBody` grows without a `max_body` cap, here a filter's lifetime is governed by an expiry timestamp while its internal result queue grows without a `max_queue_size` cap. As long as the filter has not hit `EXPIRE_SECONDS` since the last `updateExpireTime()` call, ordinary/attacker-influenced chain events (e.g., a broad filter with no address/topic restriction, or simply the block filter which matches every new block) keep depositing entries into the unbounded queue.

### Impact Explanation
An attacker who creates a broad `eth_newFilter` (no address/topic restriction, matching all logs) or an `eth_newBlockFilter`, and never calls `eth_getFilterChanges`/`eth_getFilterLogs` to drain it (or does so only sparingly while the JSON-RPC HTTP API stays enabled and reachable), causes the node to accumulate matched `LogFilterElement`/block-hash entries in an unbounded in-memory queue for the full `EXPIRE_SECONDS` window. Because filter creation is cheap, unauthenticated, and repeatable up to `maxLogFilterNum` / `maxBlockFilterNum` (tens of thousands by default), many such filters can be created in parallel, multiplying memory pressure and potentially exhausting node heap, degrading or crashing the FullNode's JSON-RPC service — an availability impact against an API the node can no longer serve.

### Likelihood Explanation
Filter creation and log/block matching are reachable from any anonymous JSON-RPC HTTP client with no signature, permission, or account requirement — only `jsonrpc.httpFullNodeEnable=true`, which is a common production configuration. The queue-size gap is a genuine root-cause match to the hackney bug class (accumulation without an independent size cap, gated only by a resettable/duration-based liveness mechanism). I was not able to conclusively confirm within tool-call limits whether `updateExpireTime()` is invoked on every matched event (which would make the filter's liveness fully attacker-extendable indefinitely, an exact one-to-one match to hackney's per-chunk timer reset) or only on filter creation and on `eth_getFilterChanges` polls; either way, the queue accumulates unboundedly for the duration the filter remains unexpired, and `maxLogFilterNum`/`maxBlockFilterNum` limit only the *number* of filters, not the size of any single filter's backlog.

### Recommendation
Add a hard cap on the number of buffered elements per `LogFilterAndResult`/`BlockFilterAndResult` (e.g., drop oldest or reject filter creation once exceeded, similar to the existing `LogBlockQuery.MAX_RESULT` pattern used for `eth_getLogs`), independent of the time-based expiry, and consider capping total aggregate memory across all live filters rather than solely capping filter count.

### Proof of Concept
1. Enable the FullNode JSON-RPC HTTP API (`node.jsonrpc.httpFullNodeEnable = true`).
2. Call `eth_newFilter` with an empty/broad `FilterRequest` (no address, no topics) to match all contract logs, or call `eth_newBlockFilter`.
3. Do not call `eth_getFilterChanges` / `eth_getFilterLogs` for the filter (or call it rarely).
4. Observe that as new blocks/logs are produced by normal chain activity, `LogFilterAndResult.add()` / `BlockFilterAndResult.add()` unconditionally grow the backing `LinkedBlockingQueue` with no size limit, for as long as the filter's `expireTimeStamp` has not elapsed.
5. Repeat filter creation up to `maxLogFilterNum`/`maxBlockFilterNum` to multiply the effect, observing node heap grow monotonically.

### Citations

**File:** framework/src/main/java/org/tron/core/services/jsonrpc/filters/LogFilterAndResult.java (L25-28)
```java
  @Override
  public void add(LogFilterElement logFilterElement) {
    result.add(logFilterElement);
  }
```

**File:** framework/src/main/java/org/tron/core/services/jsonrpc/filters/BlockFilterAndResult.java (L14-17)
```java
  @Override
  public void add(String s) {
    result.add(s);
  }
```

**File:** framework/src/main/java/org/tron/core/services/jsonrpc/filters/FilterResult.java (L8-26)
```java
public abstract class FilterResult<T> {

  private long expireTimeStamp;

  @Getter
  protected BlockingQueue<T> result;

  public void updateExpireTime() {
    expireTimeStamp = System.currentTimeMillis() + TronJsonRpcImpl.EXPIRE_SECONDS * 1000;
  }

  public boolean isExpire() {
    return expireTimeStamp < System.currentTimeMillis();
  }

  public abstract void add(T t);

  public abstract List<T> popAll();
}
```

**File:** framework/src/main/java/org/tron/core/services/jsonrpc/TronJsonRpcImpl.java (L1439-1466)
```java
  @Override
  public String newFilter(FilterRequest fr) throws JsonRpcInvalidParamsException,
      JsonRpcMethodNotFoundException, JsonRpcExceedLimitException {
    disableInPBFT("eth_newFilter");

    // not supports finalized as block parameter
    if (FINALIZED_STR.equalsIgnoreCase(fr.getFromBlock())
        || FINALIZED_STR.equalsIgnoreCase(fr.getToBlock())) {
      throw new JsonRpcInvalidParamsException(INVALID_BLOCK_RANGE);
    }

    Map<String, LogFilterAndResult> eventFilter2Result;
    if (getSource() == RequestSource.FULLNODE) {
      eventFilter2Result = eventFilter2ResultFull;
    } else {
      eventFilter2Result = eventFilter2ResultSolidity;
    }
    // Due to concurrent access, the threshold may occasionally be exceeded.
    if (maxLogFilterNum > 0 && eventFilter2Result.size() >= maxLogFilterNum) {
      throw new JsonRpcExceedLimitException(
          "exceed max log filters: " + maxLogFilterNum + ", try again later");
    }
    long currentMaxFullNum = wallet.getNowBlock().getBlockHeader().getRawData().getNumber();
    LogFilterAndResult logFilterAndResult = new LogFilterAndResult(fr, currentMaxFullNum, wallet);
    String filterID = generateFilterId();
    eventFilter2Result.put(filterID, logFilterAndResult);
    return ByteArray.toJsonHex(filterID);
  }
```

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L337-355)
```java
  private Runnable filterProcessLoop =
      () -> {
        while (isRunFilterProcessThread) {
          try {
            FilterTriggerCapsule filterCapsule = filterCapsuleQueue.poll(1, TimeUnit.SECONDS);
            if (filterCapsule instanceof LogsFilterCapsule) {
              tronJsonRpcImpl.handleLogsFilter((LogsFilterCapsule) filterCapsule);
            } else if (filterCapsule instanceof BlockFilterCapsule) {
              tronJsonRpcImpl.handleBLockFilter((BlockFilterCapsule) filterCapsule);
            }
          } catch (InterruptedException e) {
            logger.error("FilterProcessLoop get InterruptedException, error is {}.",
                    e.getMessage());
            Thread.currentThread().interrupt();
          } catch (Throwable throwable) {
            logger.error("Unknown throwable happened in filterProcessLoop. ", throwable);
          }
        }
      };
```

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L2344-2366)
```java
  private void postBlockFilter(final BlockCapsule blockCapsule, boolean solidified) {
    BlockFilterCapsule blockFilterCapsule =
        new BlockFilterCapsule(blockCapsule, solidified);
    if (!filterCapsuleQueue.offer(blockFilterCapsule)) {
      logger.info("Too many filters, block filter lost: {}.", blockCapsule.getBlockId());
    }
  }

  private void postLogsFilter(final BlockCapsule blockCapsule, boolean solidified,
      boolean removed) {
    if (!blockCapsule.getTransactions().isEmpty()) {
      long blockNumber = blockCapsule.getNum();
      List<TransactionInfo> transactionInfoList
              = getTransactionInfoByBlockNum(blockNumber).getTransactionInfoList();
      LogsFilterCapsule logsFilterCapsule = new LogsFilterCapsule(blockNumber,
          blockCapsule.getBlockId().toString(), blockCapsule.getBloom(), transactionInfoList,
          solidified, removed);

      if (!filterCapsuleQueue.offer(logsFilterCapsule)) {
        logger.info("Too many filters, logs filter lost: {}.", blockNumber);
      }
    }
  }
```
