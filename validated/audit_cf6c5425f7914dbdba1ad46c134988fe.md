### Title
Unbounded per-filter result queue in `eth_newFilter` allows anonymous JSON-RPC caller to exhaust node memory - (File: framework/src/main/java/org/tron/core/services/jsonrpc/filters/LogFilterAndResult.java)

### Summary
The JSON-RPC `eth_newFilter` API caps the *number* of concurrently open filters via `maxLogFilterNum`, but it never bounds the *size* of the result queue held by each individual filter. An unauthenticated JSON-RPC client can create a broadly-matching filter, never poll it, and let it accumulate every matching log for its full 5-minute lifetime, repeating this process to sustain unbounded server-side memory growth — the same "repeated API call → uncontrolled resource growth → DoS" pattern described in the Liferay CVE-2025-43816 advisory (CWE-401 memory leak causing server unavailability).

### Finding Description
`eth_newFilter` creates a `LogFilterAndResult` and stores it keyed by a generated filter ID in `eventFilter2ResultFull`/`eventFilter2ResultSolidity`: [1](#0-0) 

The only protection is a cap on the *number* of filters (`maxLogFilterNum`), enforced with a size check on the map: [2](#0-1) 

Each filter's backing store is an unbounded `LinkedBlockingQueue` (no capacity argument, defaults to `Integer.MAX_VALUE`): [3](#0-2) 

When a new block/log event is processed, every non-expired filter whose block range and bloom filter match gets *all* matching `LogFilterElement`s appended to its queue, with no size limit or truncation: [4](#0-3) 

The queue is only drained when the client calls `eth_getFilterChanges`/`eth_getFilterLogs` (`popAll()` via `getFilterResult`), and it is only cleaned up when the filter's 5-minute expiry timer (`EXPIRE_SECONDS = 300`) is checked — which itself only happens lazily, the next time `processLogFilterEntry`/`handleBLockFilter` iterates over the map or the client calls a filter-query method: [5](#0-4) [6](#0-5) 

An attacker who opens filters with no `address`/`topics` restriction matches every log emitted by every contract on the chain. Since java-tron processes transactions/logs continuously (blocks every 3 seconds, potentially many logs per block from TVM execution across all users), an unpolled filter's queue grows for the entire 5-minute window before expiry is even checked, and the attacker can keep opening new filters up to `maxLogFilterNum` (a per-node configured count, unauthenticated) to multiply the effect. There is no per-filter or aggregate memory/queue-length limit, unlike the `logElementCache`/`blockHashCache`, which are bounded Guava caches with `maximumSize`.

### Impact Explanation
An anonymous JSON-RPC client (no signed transaction, no authentication, only network access to the node's JSON-RPC endpoint) can drive the process heap to grow unbounded by repeatedly opening broad, unpolled filters and letting them accumulate logs for their full lifetime. On a busy full node this can exhaust JVM heap, triggering `OutOfMemoryError`, GC thrashing, and ultimately node crash or the node becoming unable to serve any RPC/API traffic — a denial-of-service against the JSON-RPC service, matching the "node crash or halt" / "an API the node can no longer serve" impact bar.

### Likelihood Explanation
Reaching this requires only calling `eth_newFilter` on an exposed JSON-RPC endpoint with no filter criteria (or overly broad criteria) and then not calling `eth_getFilterChanges`/`eth_getFilterLogs`; this is trivial for any client with network access to the RPC port and needs no special privileges. The only mitigating control (`maxLogFilterNum`) limits the count of filters, not the memory consumed per filter, so it does not prevent this growth pattern.

### Recommendation
Bound each `LogFilterAndResult`/`BlockFilterAndResult` queue with a maximum element count (e.g., cap via `LinkedBlockingQueue(capacity)` or evict oldest elements once a threshold is reached), and/or actively expire and drop filters (and their accumulated data) via a background sweep instead of relying solely on lazy expiry checks triggered by other API calls or block processing.

### Proof of Concept
1. Send unauthenticated JSON-RPC requests to the node's `eth_newFilter` endpoint with an empty `FilterRequest` (no `address`, no `topics`, wide `fromBlock`/`toBlock`), repeated up to `maxLogFilterNum` times.
2. Do not call `eth_getFilterChanges` or `eth_getFilterLogs` on any of the created filter IDs.
3. As the node continues to process blocks/transactions, `handleLogsFilter` → `processLogFilterEntry` appends every matching `LogFilterElement` to each filter's unbounded `LinkedBlockingQueue` for the full 300-second (`EXPIRE_SECONDS`) window [7](#0-6) .
4. Repeat step 1 as filters expire/are replaced, sustaining continuous memory pressure until the node experiences memory exhaustion or degraded/failed RPC service.

### Citations

**File:** framework/src/main/java/org/tron/core/services/jsonrpc/TronJsonRpcImpl.java (L274-313)
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
```

**File:** framework/src/main/java/org/tron/core/services/jsonrpc/TronJsonRpcImpl.java (L1440-1466)
```java
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

**File:** framework/src/main/java/org/tron/core/services/jsonrpc/filters/LogFilterAndResult.java (L17-28)
```java
  public LogFilterAndResult(FilterRequest fr, long currentMaxBlockNum, Wallet wallet)
      throws JsonRpcInvalidParamsException {
    // eth_newFilter, no need to check block range
    this.logFilterWrapper = new LogFilterWrapper(fr, currentMaxBlockNum, wallet, false);
    result = new LinkedBlockingQueue<>();
    this.updateExpireTime();
  }

  @Override
  public void add(LogFilterElement logFilterElement) {
    result.add(logFilterElement);
  }
```

**File:** framework/src/main/java/org/tron/core/services/jsonrpc/filters/FilterResult.java (L15-21)
```java
  public void updateExpireTime() {
    expireTimeStamp = System.currentTimeMillis() + TronJsonRpcImpl.EXPIRE_SECONDS * 1000;
  }

  public boolean isExpire() {
    return expireTimeStamp < System.currentTimeMillis();
  }
```
