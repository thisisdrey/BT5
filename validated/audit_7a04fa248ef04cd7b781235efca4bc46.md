Confirmed: the `result` queue in `FilterResult` (used by both `LogFilterAndResult` and `BlockFilterAndResult`) is an unbounded `LinkedBlockingQueue` with no per-filter size cap, only a global count cap on the number of filters (`jsonRpcMaxLogFilterNum`/`jsonRpcMaxBlockFilterNum`).### Title
Unbounded per-filter result queue growth in JSON-RPC `eth_newFilter`/`eth_newBlockFilter` allows anonymous-client memory exhaustion DoS - (File: `framework/src/main/java/org/tron/core/services/jsonrpc/filters/FilterResult.java`)

### Summary
The Xen advisory describes xenstored allocating unbounded memory because guests can issue requests/watches whose responses are buffered without any size limit, only bounded by time-based cleanup. `TronJsonRpcImpl`'s filter subsystem (`eth_newFilter`, `eth_newBlockFilter`, `eth_getFilterChanges`) has the analogous property: each filter's result buffer is an unbounded queue, gated only by a *filter-count* cap and a 5-minute expiry timer, not by buffer size.

### Finding Description
`FilterResult<T>` backs every log/block filter with an unbounded `BlockingQueue<T>`: [1](#0-0) 

Both concrete implementations construct it as a plain `LinkedBlockingQueue` with no capacity bound: [2](#0-1) [3](#0-2) 

`eth_newFilter` only checks the *number of filters* against `jsonRpcMaxLogFilterNum` (default 20,000); it does not check or bound how much data a single filter can accumulate, and when created without `fromBlock`/`toBlock` it defaults to tracking *all future blocks* (`toBlock = Long.MAX_VALUE`) with no address/topic restriction required: [4](#0-3) 

Every produced block/log event is pushed into every live filter's queue in `handleLogsFilter`/`handleBLockFilter`, unconditionally appending matched elements with no cap: [5](#0-4) [6](#0-5) 

The only reclamation mechanism is a 5-minute idle expiry (`EXPIRE_SECONDS`), and expiry is only re-armed when the client actually calls `eth_getFilterChanges`/`getFilterResult` to drain it: [7](#0-6) [8](#0-7) 

The system's own log-element cache sizing comment estimates ~300,000 log elements can be produced network-wide within one 5-minute expiry window (`300s * tps(1000) * 1 log/tx`): [9](#0-8) 

An anonymous client can repeatedly call `eth_newFilter` with no parameters (or `eth_newBlockFilter`) up to the `maxLogFilterNum`/`maxBlockFilterNum` cap, never call `eth_getFilterChanges`, and each of those filters will independently accumulate the full stream of matched log elements/block hashes for up to 5 minutes before expiry — multiplying single-stream memory usage by the number of open filters (default cap 20,000 for logs, 50,000 for blocks), with no per-filter queue size limit anywhere in the code path. This mirrors the CVE's root cause: allowing an untrusted requester to cause the node to buffer responses without reading them, and to cause large numbers of "watch"-like events to accumulate, bounded only by count/time, not by memory.

### Impact Explanation
JSON-RPC is reachable by any unauthenticated network client when `node.jsonrpc.httpFullNodeEnable`/`httpSolidityEnable` is on (no signature or account permission required). Sustained abuse of `eth_newFilter`/`eth_newBlockFilter` without draining can drive heap usage to multiples of the already-large `logElementCache` (300,000 max size) per filter, times thousands of concurrently open filters, causing OutOfMemoryError and node crash — a concrete node-crash/DoS impact against the JSON-RPC-serving FullNode/SolidityNode.

### Likelihood Explanation
No authentication, signed transaction, or special privilege is required — only that the JSON-RPC HTTP endpoint be enabled (a common production configuration for FullNodes serving Ethereum-compatible tooling). The attack is a simple repeated unauthenticated HTTP call pattern (create filters, never poll), well within reach of any external client, and the rate-limiter defaults (`QpsRateLimiterAdapter qps=1000`) do not meaningfully cap sustained abuse over the 5-minute window.

### Recommendation
Bound the per-filter result queue (e.g., cap `LinkedBlockingQueue` capacity or track/limit cumulative queued element count per filter, evicting/expiring or rejecting further pushes once a per-filter or global memory budget is hit) instead of relying solely on filter-count caps and idle-time expiry.

### Proof of Concept
1. Enable `node.jsonrpc.httpFullNodeEnable = true` on a FullNode.
2. From an anonymous client, repeatedly call `eth_newFilter` with an empty `FilterRequest` (no `fromBlock`/`toBlock`/`address`/`topics`) in a loop until `jsonRpcMaxLogFilterNum` (default 20,000) is reached — see acceptance path at [4](#0-3) .
3. Never call `eth_getFilterChanges`/`eth_getFilterLogs` to drain any of them.
4. While the network continues producing blocks/transactions with logs, every live filter's `LinkedBlockingQueue` grows independently and unboundedly for up to `EXPIRE_SECONDS` (300s) per [5](#0-4) , multiplying memory usage by the number of open filters and driving the node toward OOM.

### Citations

**File:** framework/src/main/java/org/tron/core/services/jsonrpc/filters/FilterResult.java (L8-21)
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
```

**File:** framework/src/main/java/org/tron/core/services/jsonrpc/filters/LogFilterAndResult.java (L17-23)
```java
  public LogFilterAndResult(FilterRequest fr, long currentMaxBlockNum, Wallet wallet)
      throws JsonRpcInvalidParamsException {
    // eth_newFilter, no need to check block range
    this.logFilterWrapper = new LogFilterWrapper(fr, currentMaxBlockNum, wallet, false);
    result = new LinkedBlockingQueue<>();
    this.updateExpireTime();
  }
```

**File:** framework/src/main/java/org/tron/core/services/jsonrpc/filters/BlockFilterAndResult.java (L7-17)
```java
public class BlockFilterAndResult extends FilterResult<String> {

  public BlockFilterAndResult() {
    this.updateExpireTime();
    result = new LinkedBlockingQueue<>();
  }

  @Override
  public void add(String s) {
    result.add(s);
  }
```

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

**File:** framework/src/main/java/org/tron/core/services/jsonrpc/TronJsonRpcImpl.java (L217-246)
```java
  public void handleBLockFilter(BlockFilterCapsule blockFilterCapsule) {
    Iterator<Entry<String, BlockFilterAndResult>> it;

    if (blockFilterCapsule.isSolidified()) {
      it = getBlockFilter2ResultSolidity().entrySet().iterator();
    } else {
      it = getBlockFilter2ResultFull().entrySet().iterator();
    }

    if (!it.hasNext()) {
      return;
    }
    final String originalBlockHash = ByteArray.toJsonHex(blockFilterCapsule.getBlockHash());
    String cachedBlockHash;
    try {
      // compare with hashcode() first, then with equals(). If not exist, put it.
      cachedBlockHash = blockHashCache.get(originalBlockHash, () -> originalBlockHash);
    } catch (ExecutionException e) {
      logger.error("Getting/loading blockHash from cache failed", e); // never happen
      cachedBlockHash = originalBlockHash;
    }
    while (it.hasNext()) {
      Entry<String, BlockFilterAndResult> entry = it.next();
      if (entry.getValue().isExpire()) {
        it.remove();
        continue;
      }
      entry.getValue().getResult().add(cachedBlockHash);
    }
  }
```

**File:** framework/src/main/java/org/tron/core/services/jsonrpc/TronJsonRpcImpl.java (L274-314)
```java
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

**File:** framework/src/main/java/org/tron/core/services/jsonrpc/TronJsonRpcImpl.java (L1439-1465)
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
