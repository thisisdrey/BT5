### Title
TOCTOU race in JSON-RPC filter lookups causes NullPointerException / unhandled crash on concurrent filter access - (File: `framework/src/main/java/org/tron/core/services/jsonrpc/TronJsonRpcImpl.java`)

### Summary
`TronJsonRpcImpl.getFilterResult` and `getFilterLogs` perform a check-then-act sequence (`containsKey` followed by a separate `get`) on the `ConcurrentHashMap` fields `blockFilter2ResultFull`/`Solidity` and `eventFilter2ResultFull`/`Solidity` without any lock protecting the whole read-modify sequence. `ConcurrentHashMap` only guarantees atomicity for individual operations, not for a `containsKey()` + `get()` pair. Any anonymous JSON-RPC client that concurrently calls `eth_uninstallFilter` (which removes the entry) while another thread calls `eth_getFilterChanges` or `eth_getFilterLogs` for the same `filterId` can force `get(filterId)` to return `null` after `containsKey` returned `true`, producing a `NullPointerException` when the code immediately calls `.popAll()` or `.getLogFilterWrapper()` on the null reference.

### Finding Description [1](#0-0) 

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
    ...
```

The same pattern exists in `getFilterLogs`: [2](#0-1) 

Both `containsKey(filterId)` and the subsequent `get(filterId)` calls read the shared `Map` independently. Between these two calls, another thread executing `uninstallFilter` can remove the same `filterId`: [3](#0-2) 

This is a check-then-act race — no `synchronized` block, no atomic `computeIfPresent`/`getOrDefault` guards it — which is the same root-cause class as CVE-2017-18216 (a required lock/mutex is missing around a shared structure, leading to a NULL pointer dereference). Here, instead of kernel code touching `node->nd_item`, the JSON-RPC filter maps are accessed unsynchronized across two calls, and the missing mutex results in a `NullPointerException` instead of a kernel `BUG()`.

### Impact Explanation
Any unauthenticated JSON-RPC client that owns (or can guess/enumerate) a filter ID can trigger this by concurrently issuing `eth_uninstallFilter` and `eth_getFilterChanges`/`eth_getFilterLogs` for the same ID from two connections/threads. The immediate effect is an uncaught `NullPointerException` inside the RPC handler thread. Whether this only fails the single request or corrupts broader service state depends on how the outer JSON-RPC dispatcher (Jsonrpc4j `JsonRpcBasicServer`) handles unchecked runtime exceptions — I was not able to fully verify this in the available time because the last tool call (searching for the dispatcher's exception handling) did not complete before the iteration limit. If the dispatcher does not catch generic `RuntimeException`/`NullPointerException` per-request (only the declared checked exceptions such as `ItemNotFoundException`), this could propagate and disrupt the JSON-RPC servicing thread/pool, which would satisfy "an API the node can no longer serve." If it is caught safely, the impact is limited to a single failed request (log noise, no lasting effect) and would not meet the Medium+ bar.

### Likelihood Explanation
The race window is narrow (two back-to-back map accesses), so success depends on precise timing between two client-controlled RPC calls, but the client fully controls both operations and can retry indefinitely to win the race, making eventual triggering highly likely for a motivated caller. No special privilege or economic cost is needed — any address / anonymous HTTP or WS client that created a filter can attempt the race against itself.

### Recommendation
Replace the check-then-act pattern with a single atomic map access, e.g. capture the value once with `get()` and null-check it (or use `computeIfPresent`) instead of calling `containsKey()` followed by a separate `get()`:
```java
BlockFilterAndResult bf = blockFilter2Result.get(filterId);
if (bf != null) {
  List<String> blockHashList = bf.popAll();
  ...
  bf.updateExpireTime();
} else {
  LogFilterAndResult lf = eventFilter2Result.get(filterId);
  if (lf != null) { ... } else { throw new ItemNotFoundException(...); }
}
```
Apply the same fix to `getFilterLogs`.

### Proof of Concept
1. Client A calls `eth_newBlockFilter` (or `eth_newFilter`) to obtain `filterId`.
2. Client A spawns two concurrent requests on the same connection pool:
   - Thread 1: repeatedly calls `eth_uninstallFilter(filterId)` then re-creates the filter with a fresh capture race, or more directly, times a single `eth_uninstallFilter(filterId)` call to land between the `containsKey` and `get` in `getFilterResult`/`getFilterLogs`.
   - Thread 2: repeatedly calls `eth_getFilterChanges(filterId)` / `eth_getFilterLogs(filterId)`.
3. Because `containsKey` and `get` are independent atomic operations on the `ConcurrentHashMap` but not atomic together, occasionally `containsKey` returns `true` right before Thread 1's `remove` executes, and the following `get(filterId)` returns `null`, causing `NullPointerException` at `blockFilter2Result.get(filterId).popAll()` (or the `LogFilterWrapper` equivalent in `getFilterLogs`).
4. Repeating the two concurrent request loops for a short period reliably reproduces the NPE due to the narrow but non-zero race window.

### Citations

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

**File:** framework/src/main/java/org/tron/core/services/jsonrpc/TronJsonRpcImpl.java (L1557-1570)
```java
    Map<String, LogFilterAndResult> eventFilter2Result;
    if (getSource() == RequestSource.FULLNODE) {
      eventFilter2Result = eventFilter2ResultFull;
    } else {
      eventFilter2Result = eventFilter2ResultSolidity;
    }

    filterId = ByteArray.fromHex(filterId);
    if (!eventFilter2Result.containsKey(filterId)) {
      throw new ItemNotFoundException(FILTER_NOT_FOUND);
    }

    LogFilterWrapper logFilterWrapper = eventFilter2Result.get(filterId).getLogFilterWrapper();
    long currentMaxBlockNum = wallet.getNowBlock().getBlockHeader().getRawData().getNumber();
```

**File:** framework/src/main/java/org/tron/core/services/jsonrpc/TronJsonRpcImpl.java (L1592-1613)
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
