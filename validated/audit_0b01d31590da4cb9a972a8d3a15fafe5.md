### Title
JSON-RPC log/block filters accumulate unbounded results in memory before TTL expiry, enabling remote memory-exhaustion DoS - ([File: framework/src/main/java/org/tron/core/services/jsonrpc/TronJsonRpcImpl.java])

### Summary
Any anonymous JSON-RPC client can call `eth_newFilter` (and `eth_newBlockFilter`) to register up to `maxLogFilterNum` (default cap referenced as 20,000 in code comments) filters against the node. Each filter's matched results are stored in an **unbounded** `LinkedBlockingQueue` that is only drained when the client actively polls (`eth_getFilterChanges`/`eth_getFilterLogs`), and the filter itself is only reclaimed after a fixed 5-minute idle TTL. A client can create many "match-everything" filters, never poll them, and let the block-processing pipeline continuously append matching `LogFilterElement`/block-hash entries to every filter's queue for the full TTL window, driving unbounded heap growth analogous to the MongoDB report's "internal process persists longer than anticipated, increasing memory consumption."

### Finding Description
`TronJsonRpcImpl` maintains four maps of filter state keyed by filter id: `eventFilter2ResultFull`, `eventFilter2ResultSolidity`, `blockFilter2ResultFull`, `blockFilter2ResultSolidity` [1](#0-0) .

`eth_newFilter` only bounds the *number* of filter objects via `maxLogFilterNum`, not the memory each filter can accumulate: [2](#0-1) 

`eth_newBlockFilter` similarly only bounds filter *count* via `maxBlockFilterNum`: [3](#0-2) 

Each filter's backing store is an unbounded `LinkedBlockingQueue` created without any capacity limit: [4](#0-3) [5](#0-4) 

For every processed block, `handleLogsFilter` iterates all live filters in `eventFilter2ResultFull`/`eventFilter2ResultSolidity` and appends every matching log element into the filter's queue via `processLogFilterEntry`, regardless of whether the client has ever polled the filter: [6](#0-5) 

The queue is only drained on `getFilterChanges`/`getFilterLogs` → `getFilterResult`, and the filter's expiry timer is only refreshed on that same client-driven access: [7](#0-6) [8](#0-7) 

The only cleanup mechanism for an unpolled filter is expiry after `EXPIRE_SECONDS` (5 minutes) of *inactivity from creation*, checked lazily inside `processLogFilterEntry` during the next block's `handleLogsFilter` pass: [9](#0-8) 

This means an attacker can register up to `maxLogFilterNum` wildcard filters (no address/topic restriction matches every emitted log) and never poll them. For the full 5-minute window, every one of those filters accumulates a full copy of every matching log across every processed block, multiplying memory usage by filter count × logs-per-block × blocks-in-window before eviction ever occurs, and the attacker can repeat the cycle indefinitely by re-registering filters as old ones expire.

### Impact Explanation
This can drive the fullnode/solidity-node JVM heap usage far beyond expectations, leading to `OutOfMemoryError`, GC thrashing, and node crash/halt — denying the JSON-RPC service and potentially destabilizing the node process entirely. This matches the "Accept only... node crash or halt... or an API the node can no longer serve" bar from the validation rules.

### Likelihood Explanation
`eth_*` JSON-RPC filter methods are typically exposed without authentication on public/RPC-provider nodes (per the code, only gated by PBFT-mode check via `disableInPBFT`, not by any auth/rate limiting) [10](#0-9) . Any remote unauthenticated client can trigger this pattern with ordinary API calls (`eth_newFilter` with empty criteria, repeated up to the cap, and simply not calling `eth_getFilterChanges`), making exploitation straightforward and requiring no special privileges.

### Recommendation
- Bound the per-filter result queue (e.g., cap `LinkedBlockingQueue` capacity or total bytes) and drop/evict oldest entries or reject/expire the filter early once the cap is exceeded, rather than growing without bound.
- Track aggregate memory/entry count across all live filters and enforce a global ceiling, not just a filter-count ceiling.
- Consider proactively expiring filters based on unconsumed backlog size, not purely idle-time, and/or reducing `EXPIRE_SECONDS` for filters with large unconsumed backlogs.

### Proof of Concept
1. Send repeated `eth_newFilter` JSON-RPC requests with an empty `FilterRequest` (no address/topics) until reaching `maxLogFilterNum` active filters (default referenced as 20,000).
2. Do not call `eth_getFilterChanges` or `eth_getFilterLogs` for any of them.
3. Drive/await normal chain activity (or submit many TRC20/log-emitting transactions) for the 5-minute TTL window; each block's `handleLogsFilter` call appends every matching log to every one of the thousands of unpolled filters' unbounded queues [11](#0-10) .
4. Repeat the filter-creation cycle as old filters expire, sustaining continuous memory growth until the node OOMs or GC-stalls, denying the JSON-RPC API (and potentially crashing the node process).

### Citations

**File:** framework/src/main/java/org/tron/core/services/jsonrpc/TronJsonRpcImpl.java (L124-132)
```java
  private static final String FILTER_NOT_FOUND = "filter not found";
  public static final int EXPIRE_SECONDS = 5 * 60;
  private final int maxBlockFilterNum = Args.getInstance().getJsonRpcMaxBlockFilterNum();
  private final int maxLogFilterNum = Args.getInstance().getJsonRpcMaxLogFilterNum();
  private static final Cache<LogFilterElement, LogFilterElement> logElementCache =
      CacheBuilder.newBuilder()
          .maximumSize(300_000L) // 300s * tps(1000) * 1 log/tx ≈ 300_000
          .expireAfterWrite(EXPIRE_SECONDS, TimeUnit.SECONDS)
          .recordStats().build(); //LRU cache
```

**File:** framework/src/main/java/org/tron/core/services/jsonrpc/TronJsonRpcImpl.java (L138-161)
```java
  /**
   * for log filter in Full Json-RPC
   */
  @Getter
  private final Map<String, LogFilterAndResult> eventFilter2ResultFull =
      new ConcurrentHashMap<>();
  /**
   * for block in Full Json-RPC
   */
  @Getter
  private final Map<String, BlockFilterAndResult> blockFilter2ResultFull =
      new ConcurrentHashMap<>();
  /**
   * for log filter in solidity Json-RPC
   */
  @Getter
  private final Map<String, LogFilterAndResult> eventFilter2ResultSolidity =
      new ConcurrentHashMap<>();
  /**
   * for block in solidity Json-RPC
   */
  @Getter
  private final Map<String, BlockFilterAndResult> blockFilter2ResultSolidity =
      new ConcurrentHashMap<>();
```

**File:** framework/src/main/java/org/tron/core/services/jsonrpc/TronJsonRpcImpl.java (L248-314)
```java
  /**
   * append LogsFilterCapsule's LogFilterElement list to each filter if matched
   */
  public void handleLogsFilter(LogsFilterCapsule logsFilterCapsule) {
    long t1 = System.currentTimeMillis();
    Map<String, LogFilterAndResult> eventFilterMap;

    if (logsFilterCapsule.isSolidified()) {
      eventFilterMap = getEventFilter2ResultSolidity();
    } else {
      eventFilterMap = getEventFilter2ResultFull();
    }

    if (eventFilterMap.size() <= filterParallelThreshold) {
      eventFilterMap.entrySet().forEach(
          entry -> processLogFilterEntry(entry, eventFilterMap, logsFilterCapsule));
    } else {
      logsFilterPool.submit(() -> eventFilterMap.entrySet().parallelStream()
          .forEach(entry -> processLogFilterEntry(entry, eventFilterMap, logsFilterCapsule))
      ).join();
    }
    long t2 = System.currentTimeMillis();
    logger.debug("handleLogsFilter {} cost {}, filter size {}",
        logsFilterCapsule.isSolidified() ? "Solidity" : "Full", t2 - t1, eventFilterMap.size());
  }

  private void processLogFilterEntry(
      Map.Entry<String, LogFilterAndResult> entry,
      Map<String, LogFilterAndResult> eventFilterMap,
      LogsFilterCapsule logsFilterCapsule) {
    LogFilterAndResult logFilterAndResult = entry.getValue();
    if (logFilterAndResult.isExpire()) {
      eventFilterMap.remove(entry.getKey());
      return;
    }

    long blockNumber = logsFilterCapsule.getBlockNumber();
    long fromBlock = logFilterAndResult.getLogFilterWrapper().getFromBlock();
    long toBlock = logFilterAndResult.getLogFilterWrapper().getToBlock();
    if (!(fromBlock <= blockNumber && blockNumber <= toBlock)) {
      return;
    }

    if (logsFilterCapsule.getBloom() != null && !logFilterAndResult.getLogFilterWrapper()
        .getLogFilter().matchBloom(logsFilterCapsule.getBloom())) {
      return;
    }

    LogFilter logFilter = logFilterAndResult.getLogFilterWrapper().getLogFilter();
    List<LogFilterElement> elements =
        LogMatch.matchBlock(logFilter, blockNumber, logsFilterCapsule.getBlockHash(),
            logsFilterCapsule.getTxInfoList(), logsFilterCapsule.isRemoved());

    List<LogFilterElement> localResults = new ArrayList<>(elements.size());
    for (LogFilterElement element : elements) {
      LogFilterElement cachedElement;
      try {
        // compare with hashcode() first, then with equals(). If not exist, put it.
        cachedElement = logElementCache.get(element, () -> element);
      } catch (ExecutionException e) {
        logger.error("Getting/loading LogFilterElement from cache fails", e); // never happen
        cachedElement = element;
      }
      localResults.add(cachedElement);
    }
    logFilterAndResult.getResult().addAll(localResults);
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

**File:** framework/src/main/java/org/tron/core/services/jsonrpc/TronJsonRpcImpl.java (L1468-1488)
```java
  @Override
  public String newBlockFilter() throws JsonRpcMethodNotFoundException,
      JsonRpcExceedLimitException {
    disableInPBFT("eth_newBlockFilter");

    Map<String, BlockFilterAndResult> blockFilter2Result;
    if (getSource() == RequestSource.FULLNODE) {
      blockFilter2Result = blockFilter2ResultFull;
    } else {
      blockFilter2Result = blockFilter2ResultSolidity;
    }
    if (maxBlockFilterNum > 0 && blockFilter2Result.size() >= maxBlockFilterNum) {
      throw new JsonRpcExceedLimitException(
          "exceed max block filters: " + maxBlockFilterNum + ", try again later");
    }

    BlockFilterAndResult filterAndResult = new BlockFilterAndResult();
    String filterID = generateFilterId();
    blockFilter2Result.put(filterID, filterAndResult);
    return ByteArray.toJsonHex(filterID);
  }
```

**File:** framework/src/main/java/org/tron/core/services/jsonrpc/TronJsonRpcImpl.java (L1593-1613)
```java
  public Object[] getFilterResult(String filterId, Map<String, BlockFilterAndResult>
      blockFilter2Result, Map<String, LogFilterAndResult> eventFilter2Result)
      throws ItemNotFoundException {
    Object[] result;

    if (blockFilter2Result.containsKey(filterId)) {
      List<String> blockHashList = blockFilter2Result.get(filterId).popAll();
      result = blockHashList.toArray(new String[blockHashList.size()]);
      blockFilter2Result.get(filterId).updateExpireTime();

    } else if (eventFilter2Result.containsKey(filterId)) {
      List<LogFilterElement> logElementList = eventFilter2Result.get(filterId).popAll();
      result = logElementList.toArray(new LogFilterElement[0]);
      eventFilter2Result.get(filterId).updateExpireTime();

    } else {
      throw new ItemNotFoundException(FILTER_NOT_FOUND);
    }

    return result;
  }
```

**File:** framework/src/main/java/org/tron/core/services/jsonrpc/filters/LogFilterAndResult.java (L12-23)
```java
public class LogFilterAndResult extends FilterResult<LogFilterElement> {

  @Getter
  private final LogFilterWrapper logFilterWrapper;

  public LogFilterAndResult(FilterRequest fr, long currentMaxBlockNum, Wallet wallet)
      throws JsonRpcInvalidParamsException {
    // eth_newFilter, no need to check block range
    this.logFilterWrapper = new LogFilterWrapper(fr, currentMaxBlockNum, wallet, false);
    result = new LinkedBlockingQueue<>();
    this.updateExpireTime();
  }
```

**File:** framework/src/main/java/org/tron/core/services/jsonrpc/filters/BlockFilterAndResult.java (L7-12)
```java
public class BlockFilterAndResult extends FilterResult<String> {

  public BlockFilterAndResult() {
    this.updateExpireTime();
    result = new LinkedBlockingQueue<>();
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
