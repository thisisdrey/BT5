### Title
Native RocksDB iterator resource leak in `Wallet.getMarketPairList()` via early `break` without close - (File: `framework/src/main/java/org/tron/core/Wallet.java`)

### Summary
`Wallet.getMarketPairList()` obtains a `DBIterator` from `MarketPairToPriceStore.iterator()` and iterates it in a plain `while (iterator.hasNext())` loop that breaks out early once `count > MARKET_COUNT_LIMIT_MAX`, without ever calling `iterator.close()`. This is the same bug class as CVE-2022-49185 (missing release of a reference-counted/native resource on an early-exit path): the returned `Iterator` wraps a `RockStoreIterator`, which internally holds a native `RocksIterator` and `ReadOptions` object that are only released either by full exhaustion (`hasNext()` returning `false`, which triggers an internal `close()`) or by an explicit `close()` call. [1](#0-0) 

### Finding Description
`RockStoreIterator` (the concrete `DBIterator` returned by RocksDB-backed stores) only releases its native `ReadOptions`/`RocksIterator` handles in `close()`, and that `close()` is normally triggered implicitly only when `hasNext()` observes the iterator has become invalid (i.e., full exhaustion): [2](#0-1) 

`TronStoreWithRevoking.iterator()` returns exactly this kind of iterator wrapped via `Iterators.transform`, so callers of `Store.iterator()` receive an object that must be closed explicitly if it is not fully drained: [3](#0-2) 

In `Wallet.getMarketPairList()`, the loop breaks as soon as `count > MARKET_COUNT_LIMIT_MAX` is reached, leaving the underlying `RocksIterator`/`ReadOptions` native objects un-freed for the lifetime of the JVM (until GC finalization, if any, eventually reclaims them — RocksJava objects rely on explicit `close()`/finalizers that are not guaranteed to run promptly): [4](#0-3) 

This directly mirrors the kernel bug's root cause: an early-return/break path that skips releasing a resource obtained earlier in the function, which was otherwise correctly released on the "normal" path.

### Impact Explanation
`getMarketPairList()` is reachable by any anonymous API client with no authentication or signature required, via the HTTP servlet `GetMarketPairListServlet` (`doGet`/`doPost`) and the corresponding gRPC service methods on the full node, PBFT node, and Solidity node: [5](#0-4) 

Every request whose market-pair count exceeds `MARKET_COUNT_LIMIT_MAX` leaks a native RocksDB iterator/ReadOptions handle. Repeated invocation (trivially automatable, no cost to the caller beyond an HTTP/gRPC request) accumulates un-freed native (off-heap) memory and open RocksDB iterator handles over time, which can degrade RocksDB performance and, if left running, exhaust native memory or hit internal RocksDB iterator-count limits, leading to node instability/crash — a resource-exhaustion DoS against the query-serving node.

### Likelihood Explanation
High: the endpoint requires no authentication, no fee, and no special privileges — a single unauthenticated HTTP/gRPC query is sufficient to trigger the leak once the on-chain market pair count exceeds `MARKET_COUNT_LIMIT_MAX` (a value that can be pushed up by placing enough market orders, which is itself a normal unprivileged operation). Repeated calls scale the leak linearly with request volume.

### Recommendation
Wrap the iterator usage in `Wallet.getMarketPairList()` in a try-with-resources block (the returned type from `Store.iterator()` implements `Closeable`/`AutoCloseable` via `DBIterator`), ensuring `close()` is invoked on every exit path including the early `break`, consistent with the documented usage pattern already called out in the codebase (`RocksDbDataSourceImpl.iterator()` Javadoc) and already followed correctly in other call sites such as `RocksDbDataSourceImpl.prefixQuery`.

### Proof of Concept
1. Place enough market sell orders on-chain (via `MarketSellAssetContract`) so that the number of distinct market pairs stored in `MarketPairToPriceStore` exceeds `MARKET_COUNT_LIMIT_MAX`.
2. Repeatedly send unauthenticated HTTP requests to `GET/POST /wallet/getmarketpairlist` (or the equivalent gRPC `GetMarketPairList` call) on the full node.
3. Each call executes `Wallet.getMarketPairList()`, which breaks out of the iteration loop once `count > MARKET_COUNT_LIMIT_MAX` without closing the iterator, leaking the native `RocksIterator`/`ReadOptions` handle.
4. Repeat step 2 at volume; observe growing native memory usage / open file handle count for the RocksDB instance until the node experiences degraded performance or crashes from resource exhaustion.

### Citations

**File:** framework/src/main/java/org/tron/core/Wallet.java (L2883-2903)
```java
  public MarketOrderPairList getMarketPairList() {
    MarketOrderPairList.Builder builder = MarketOrderPairList.newBuilder();
    MarketPairToPriceStore marketPairToPriceStore = dbManager.getChainBaseManager()
        .getMarketPairToPriceStore();

    Iterator<Entry<byte[], BytesCapsule>> iterator = marketPairToPriceStore
        .iterator();
    long count = 0;
    while (iterator.hasNext()) {
      Entry<byte[], BytesCapsule> next = iterator.next();

      byte[] pairKey = next.getKey();
      builder.addOrderPair(MarketUtils.decodeKeyToMarketPairHuman(pairKey));
      count++;
      if (count > MARKET_COUNT_LIMIT_MAX) {
        break;
      }
    }

    return builder.build();
  }
```

**File:** chainbase/src/main/java/org/tron/core/db/common/iterator/RockStoreIterator.java (L26-58)
```java
  @Override
  public void close() throws IOException {
    if (close.compareAndSet(false, true)) {
      readOptions.close();
      dbIterator.close();
    }
  }

  @Override
  public boolean hasNext() {
    if (close.get()) {
      return false;
    }
    boolean hasNext = false;
    // true is first item
    try {
      if (first) {
        dbIterator.seekToFirst();
        first = false;
      }
      if (!(hasNext = dbIterator.isValid())) { // false is last item
        close();
      }
    } catch (Exception e) {
      logger.error(e.getMessage(), e);
      try {
        close();
      } catch (Exception e1) {
        logger.error(e1.getMessage(), e1);
      }
    }
    return hasNext;
  }
```

**File:** chainbase/src/main/java/org/tron/core/db/TronStoreWithRevoking.java (L184-193)
```java
  @Override
  public Iterator<Map.Entry<byte[], T>> iterator() {
    return Iterators.transform(revokingDB.iterator(), e -> {
      try {
        return Maps.immutableEntry(e.getKey(), of(e.getValue()));
      } catch (BadItemException e1) {
        throw new RuntimeException(e1);
      }
    });
  }
```

**File:** framework/src/main/java/org/tron/core/services/http/GetMarketPairListServlet.java (L20-53)
```java
  protected void doGet(HttpServletRequest request, HttpServletResponse response) {
    try {
      boolean visible = Util.getVisible(request);
      fillResponse(visible, response);
    } catch (Exception e) {
      Util.processError(e, response);
    }
  }

  protected void doPost(HttpServletRequest request, HttpServletResponse response) {
    try {
      String input = request.getReader().lines()
          .collect(Collectors.joining(System.lineSeparator())).trim();
      Util.checkBodySize(input);

      boolean visible = false;
      if (!"".equals(input)) {
        visible = Util.getVisiblePost(input);
      }
      fillResponse(visible, response);
    } catch (Exception e) {
      Util.processError(e, response);
    }
  }

  private void fillResponse(boolean visible, HttpServletResponse response)
      throws Exception {
    MarketOrderPairList reply = wallet.getMarketPairList();
    if (reply != null) {
      response.getWriter().println(JsonFormat.printToString(reply, visible));
    } else {
      response.getWriter().println("{}");
    }
  }
```
