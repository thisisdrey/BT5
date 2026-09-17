### Title
Time-of-check/time-of-use race in `TronJsonRpcImpl` filter maps causes NullPointerException on concurrent JSON-RPC filter access - ([File: framework/src/main/java/org/tron/core/services/jsonrpc/TronJsonRpcImpl.java])

### Summary
`TronJsonRpcImpl` stores active `eth_newFilter`/`eth_newBlockFilter` filters in `ConcurrentHashMap`s, but several methods perform a `containsKey()` check followed by a separate, unsynchronized `get()` call (or multiple `get()` calls) on the same map without any lock protecting the pair of operations. A concurrent `eth_uninstallFilter` call, or the internal expired-filter cleanup that runs from block/log processing, can remove the entry between the check and the use, causing `NullPointerException` when the returned `null` is dereferenced. This is analogous to CVE-2023-28466: a missing lock around a getter that reads a mutable, concurrently-modifiable object leads to a race condition and a NULL pointer dereference.

### Finding Description
`getFilterResult()` — used by `eth_getFilterChanges` — does: [1](#0-0) 

```java
if (blockFilter2Result.containsKey(filterId)) {
  List<String> blockHashList = blockFilter2Result.get(filterId).popAll();
  ...
  blockFilter2Result.get(filterId).updateExpireTime();
} else if (eventFilter2Result.containsKey(filterId)) {
  ...
}
```

Between the `containsKey` check and the two subsequent `get` calls, the entry can be removed by:
- A concurrent `eth_uninstallFilter` request on the same `filterId`, which calls `eventFilter2Result.remove(filterId)` / `blockFilter2Result.remove(filterId)` with no coordination with `getFilterResult` [2](#0-1) .
- The internal expiry-cleanup path triggered by block/log events, which iterates the same map and calls `it.remove()` / `eventFilterMap.remove(entry.getKey())` for expired filters, again with no lock shared with readers [3](#0-2) [4](#0-3) .

`ConcurrentHashMap` guarantees thread-safety of each individual operation, but not the atomicity of the `containsKey()` + `get()` pair used here. When the intervening removal wins the race, `.get(filterId)` returns `null`, and the immediate `.popAll()` / `.updateExpireTime()` call throws an unhandled `NullPointerException` — mirroring the kernel bug class where a missing lock around a state read allows another thread to invalidate the referenced object first, producing a NULL-pointer dereference.

### Impact Explanation
Any anonymous JSON-RPC client can trigger this by calling `eth_getFilterChanges`/`eth_getFilterLogs` for a filter ID that is concurrently expiring or being uninstalled (by itself or another client, since filter IDs are only protected by knowledge of the ID string). The resulting NPE is thrown from within request processing, causing that request to fail with an internal error. Because the race window recurs on every block/log event that triggers expiry cleanup while filter accessor calls are in flight, this can be reliably reproduced by an unprivileged JSON-RPC API client, degrading the reliability of the `eth_getFilterChanges`/`eth_getFilterLogs`/`eth_uninstallFilter` API surface.

### Likelihood Explanation
Both the removal paths (block event driven expiry cleanup in `handleBLockFilter`/`handleLogsFilter`, and explicit `eth_uninstallFilter`) run continuously/frequently in a live full node, and the filter map is queried by any client with the filter ID. No special privileges are required, and the race window opens on ordinary node operation (new blocks arrive roughly every 3 seconds), making the race practically triggerable, especially by an attacker who calls `getFilterChanges` in a tight loop while its own filter approaches expiry or issuing `uninstallFilter` concurrently with `getFilterChanges` for the same ID.

### Recommendation
Make check-and-use of filter entries atomic, e.g. replace the `containsKey`+`get` pattern with a single `get()` call and null-check the result, or use `Map.compute`/`computeIfPresent` to atomically fetch-and-update the filter's state:
```java
BlockFilterAndResult bf = blockFilter2Result.get(filterId);
if (bf != null) {
  List<String> blockHashList = bf.popAll();
  bf.updateExpireTime();
  return ...
}
LogFilterAndResult ef = eventFilter2Result.get(filterId);
if (ef != null) {
  ...
}
throw new ItemNotFoundException(FILTER_NOT_FOUND);
```
Apply the same fix pattern to `uninstallFilter()` and `getFilterLogs()` wherever `containsKey` is followed by a separate `get`.

### Proof of Concept
1. Client A creates a filter via `eth_newFilter`/`eth_newBlockFilter`, obtaining `filterId`.
2. Client A repeatedly calls `eth_getFilterChanges(filterId)`.
3. Concurrently, either:
   - Client B (or A) calls `eth_uninstallFilter(filterId)`, or
   - the node produces a new block, triggering `handleBLockFilter`/`handleLogsFilter`, which removes the filter once its `EXPIRE_SECONDS` (5 minutes) window elapses.
4. When the removal happens between `containsKey(filterId)` and the subsequent `get(filterId)` call inside `getFilterResult`, the JSON-RPC request thread throws `NullPointerException` instead of the expected `ItemNotFoundException`, evidencing the unsynchronized check-then-act race.

### Citations

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

**File:** framework/src/main/java/org/tron/core/services/jsonrpc/TronJsonRpcImpl.java (L274-282)
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
```

**File:** framework/src/main/java/org/tron/core/services/jsonrpc/TronJsonRpcImpl.java (L1490-1515)
```java
  @Override
  public boolean uninstallFilter(String filterId) throws ItemNotFoundException,
      JsonRpcMethodNotFoundException {
    disableInPBFT("eth_uninstallFilter");

    Map<String, BlockFilterAndResult> blockFilter2Result;
    Map<String, LogFilterAndResult> eventFilter2Result;
    if (getSource() == RequestSource.FULLNODE) {
      blockFilter2Result = blockFilter2ResultFull;
      eventFilter2Result = eventFilter2ResultFull;
    } else {
      blockFilter2Result = blockFilter2ResultSolidity;
      eventFilter2Result = eventFilter2ResultSolidity;
    }

    filterId = ByteArray.fromHex(filterId);
    if (eventFilter2Result.containsKey(filterId)) {
      eventFilter2Result.remove(filterId);
    } else if (blockFilter2Result.containsKey(filterId)) {
      blockFilter2Result.remove(filterId);
    } else {
      throw new ItemNotFoundException(FILTER_NOT_FOUND);
    }

    return true;
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
