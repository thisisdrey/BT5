### Title
Unbounded per-filter result queue growth via anonymous `eth_newFilter` causes JSON-RPC server heap exhaustion - ([File: framework/src/main/java/org/tron/core/services/jsonrpc/filters/FilterResult.java])

### Summary
An anonymous JSON-RPC client can call `eth_newFilter` (no signature or authentication required) and then simply never poll it. Every subsequent matching block/log causes the node to unconditionally append data into that filter's `result` queue, which is an unbounded `LinkedBlockingQueue`. The allocation is only reclaimed when the client calls `eth_getFilterChanges`/`eth_getFilterLogs` (`popAll()`), which a malicious client will never do — directly mirroring the QUIC `PATH_CHALLENGE`/`PATH_RESPONSE` bug class: the node allocates memory per remote-triggered event and frees it only upon an action the malicious remote party controls and can withhold.

### Finding Description
`eth_newFilter` creates a `LogFilterAndResult` whose backing store is an unbounded `LinkedBlockingQueue<LogFilterElement>`: [1](#0-0) 

Every time the node processes a block, `handleLogsFilter` iterates all live filters and, for any that structurally match (address/topic/bloom), appends the matched `LogFilterElement`s to that filter's queue with no cap on how many can accumulate: [2](#0-1) 

Memory is only released when the client calls `getFilterChanges`/`getFilterLogs`, which drains the queue via `popAll()`: [3](#0-2) 

The only safeguard is the total *number* of filters (`node.jsonrpc.maxLogFilterNum`, default 20000) and a fixed-duration TTL (`EXPIRE_SECONDS`) enforced in `FilterResult.isExpire()`/`updateExpireTime()`: [4](#0-3) 

Neither mechanism bounds the *size* of an individual filter's result queue while it is alive. A broad filter (empty address/topics, matching all logs) that is created and never polled will keep absorbing every matching log/tx from every processed block for the entire TTL window, and an attacker can create up to `maxLogFilterNum` such filters concurrently (each creation is a single unauthenticated HTTP/JSON-RPC POST), then continuously replace expiring ones with fresh filters to sustain the memory pressure indefinitely. This is directly analogous to the CVE: an unbounded per-request server-side allocation that is reclaimed only via an action the remote party (here, "polling" analogous to "ACK") controls and can simply never perform.

### Impact Explanation
This can exhaust the JSON-RPC-enabled node's heap, leading to `OutOfMemoryError`, GC thrashing, or crash — a Denial of Service against a public FullNode/SolidityNode JSON-RPC endpoint, satisfying the "node crash / API the node can no longer serve" impact bar. No signature or account balance is needed to trigger it — pure unauthenticated HTTP/JSON-RPC access to `eth_newFilter` and normal transaction broadcasting (to generate matching logs) suffice.

### Likelihood Explanation
Any node operator running the `node.jsonrpc` HTTP endpoints (`httpFullNodeEnable`/`httpSolidityEnable`) is exposed. Triggering requires no special privileges, only standard JSON-RPC calls plus ordinary log-emitting transactions (which the attacker can also generate themselves, e.g., cheap contract calls emitting events), making this reasonably easy to reproduce and sustain at scale (bounded only by `maxLogFilterNum` filters × block-processing rate × TTL window, repeated).

### Recommendation
Impose a maximum size (or maximum accumulated byte/element count) per `LogFilterAndResult`/`BlockFilterAndResult` result queue in `FilterResult`/`LogFilterAndResult.add()`/`processLogFilterEntry`, evicting the filter (or refusing further additions) once the cap is hit, similar to how OpenSSL now bounds/limits queued `PATH_RESPONSE` frames per connection.

### Proof of Concept
1. Enable JSON-RPC on a FullNode (`node.jsonrpc.httpFullNodeEnable = true`).
2. Send `eth_newFilter` with an empty `FilterRequest` (no address/topics) — this matches every log on the chain — repeated up to `maxLogFilterNum` (default 20000) times, never calling `eth_getFilterChanges`/`eth_getFilterLogs`.
3. Separately, broadcast (or just wait for normal chain activity to produce) many log-emitting transactions.
4. Observe `eventFilter2ResultFull`/`eventFilter2ResultSolidity` map entries' internal `LinkedBlockingQueue` grow unbounded across the filter TTL window as shown by `processLogFilterEntry` appending on every matching block, only reset by `popAll()` in `getFilterResult`, which the attacker never calls.
5. Repeat by re-creating filters as old ones expire to sustain elevated heap usage indefinitely.

### Citations

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

**File:** framework/src/main/java/org/tron/core/services/jsonrpc/TronJsonRpcImpl.java (L296-313)
```java
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
