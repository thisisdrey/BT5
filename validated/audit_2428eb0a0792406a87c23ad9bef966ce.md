### Title
Unhandled `IndexOutOfBoundsException` in `MarketPairPriceToOrderStore.getPriceKeysList` crashes transaction/block processing - (File: chainbase/src/main/java/org/tron/core/store/MarketPairPriceToOrderStore.java)

### Summary
The reported issue is that `Keep3rV1Oracle.sol` indexes into `observations[pair]` without guaranteeing the array has at least the assumed length, which can revert unexpectedly. The closest reachable analog in java-tron is `MarketPairPriceToOrderStore.getPriceKeysList`, which performs `subList(1, (int)(limit + 1))` on a list returned from `getKeysNext` without verifying the list actually contains at least `limit + 1` elements.

### Finding Description
`getPriceKeysList` calls:
```java
result = getKeysNext(headKey, limit + 1).subList(1, (int)(limit + 1));
``` [1](#0-0) 

This assumes `getKeysNext` always returns a list of size `limit + 1` (or more) whenever `has(headKey)` is true, so it can safely call `subList(1, limit+1)`. No length check is performed before the slice.

`getKeysNext` is implemented in `Chainbase.getKeysNext` for the Market stores, which merges an uncommitted-snapshot key set with a raw LevelDB/RocksDB range scan, filters both by pair-key equality, deduplicates, and then truncates to `limit` via a stream `.limit(limit)`: [2](#0-1) 

The raw range scan is bounded by `limitLevelDB = limit + collectionList.size()` items fetched starting at `headKey` from the underlying store *before* pair filtering is applied. Because this bound is heuristic (based on the total number of uncommitted keys across the whole store, not specifically the number of keys belonging to the requested pair), and the underlying `LevelDB`/`RocksDB` layer implementations of `getKeysNext` were not inspected in full detail here, it is not proven with certainty that the returned/filtered list is always guaranteed to reach `limit + 1` size in every state (e.g., right after concurrent price-bucket insert/delete bookkeeping in `MarketPairToPriceStore`, or when the maintained price-count diverges even slightly from the physically enumerable keys for that pair). If the returned list ends up shorter than `limit + 1`, `subList(1, limit+1)` throws `IndexOutOfBoundsException`.

This method is reached directly from a signed, unprivileged `MarketSellAssetContract` transaction via `MarketSellAssetActuator.matchOrder`:
```java
List<byte[]> priceKeysList = pairPriceToOrderStore
    .getPriceKeysList(MarketUtils.getPairPriceHeadKey(makerSellTokenID, makerBuyTokenID),
        (long) (MAX_MATCH_NUM + 1), makerPriceNumber, true);
``` [3](#0-2) 

`MarketSellAssetActuator.execute` only catches `ItemNotFoundException`, `InvalidProtocolBufferException`, `BalanceInsufficientException`, and `ContractValidateException`: [4](#0-3) 

An `IndexOutOfBoundsException` is an unchecked `RuntimeException` and is **not** among these, so it would propagate out of `execute()` uncaught during transaction/block processing.

The same unguarded `getPriceKeysList`/`priceKeysList.get(0)` pattern is also used read-only from `Wallet.getMarketPriceByPair` and `Wallet.getMarketOrderListByPair`, which are exposed via gRPC/HTTP (`GetMarketPriceByPairServlet`, `RpcApiService`), giving an additional externally-reachable trigger surface: [5](#0-4) [6](#0-5) 

### Impact Explanation
If reached during block application (via `MarketSellAssetActuator.execute`), an uncaught `IndexOutOfBoundsException` would propagate through the actuator/transaction-processing pipeline in `Manager`, which can crash or halt the block-processing node, or — if only some nodes hit this path while others don't (e.g., due to timing/order-dependent state) — cause divergent processing outcomes. If reached via the read-only Wallet RPC/HTTP paths, it results in a 500/gRPC error that denies that API to callers (an API the node can no longer serve for that query), a Medium-severity availability issue; the block-processing path, if triggerable, would be High/Critical due to node crash/halt risk.

### Likelihood Explanation
The likelihood is uncertain because I could not fully verify, within tool budget, whether the underlying `LevelDbDataSourceImpl.getKeysNext` / `RocksDbDataSourceImpl.getKeysNext` implementations and the `MarketPairToPriceStore` price-count bookkeeping guarantee that the number of enumerable keys for a pair always matches or exceeds the stored count in every revoking-snapshot state. The `matchOrder` loop already anticipates edge cases (it enforces `MAX_MATCH_NUM` and removes exhausted price buckets), suggesting the original authors were aware of related boundary issues, but no explicit bounds check exists before the `subList` call in `getPriceKeysList` itself.

### Recommendation
Add an explicit bounds check in `MarketPairPriceToOrderStore.getPriceKeysList` before calling `subList`, e.g., clamp `limit` to `Math.min(limit, result.size() - 1)` or return an empty/partial list gracefully when `getKeysNext(...)` returns fewer than `limit + 1` entries, instead of assuming the list is always long enough. Additionally, wrap or validate results in `MarketSellAssetActuator.matchOrder` and in `Wallet.getMarketPriceByPair` / `getMarketOrderListByPair` so an unexpected short list degrades gracefully (e.g., treated as "no more matches") rather than throwing an unchecked exception during block application.

### Proof of Concept
Not fully reproducible from static analysis alone — reproducing this requires constructing market order/cancel sequences (`MarketSellAssetActuator`, `MarketCancelOrderActuator`) that drive `MarketPairToPriceStore`'s tracked price count and the snapshot/LevelDB key state into a mismatch (e.g., through concurrent add/remove operations across the revoking-snapshot window) such that `getKeysNext(headKey, limit+1)` returns fewer than `limit+1` keys while `has(headKey)` is still true, then issuing a `MarketSellAssetContract` transaction that triggers `matchOrder` with that mismatched pair. Given the complexity of the snapshot/LevelDB interaction, this should be validated experimentally (e.g., via a Devin session running `MarketSellAssetActuatorTest`/`MarketPairPriceToOrderStoreTest`-style scenarios with forced count/key mismatches) rather than asserted as proven here.

### Citations

**File:** chainbase/src/main/java/org/tron/core/store/MarketPairPriceToOrderStore.java (L50-64)
```java
  public List<byte[]> getPriceKeysList(byte[] headKey, long count, long totalCount, boolean skip) {
    List<byte[]> result = new ArrayList<>();

    if (has(headKey)) {
      long limit = count > totalCount ? totalCount : count;
      if (skip) {
        // need to get one more
        result = getKeysNext(headKey, limit + 1).subList(1, (int)(limit + 1));
      } else {
        result = getKeysNext(headKey, limit);
      }
    }

    return result;
  }
```

**File:** chainbase/src/main/java/org/tron/core/db2/core/Chainbase.java (L217-273)
```java
  // for market
  private List<byte[]> getKeysNext(Snapshot head, byte[] key, long limit) {
    if (limit <= 0) {
      return Collections.emptyList();
    }

    Map<WrappedByteArray, Operator> collectionList = new HashMap<>();
    if (head.getPrevious() != null) {
      ((SnapshotImpl) head).collectUnique(collectionList);
    }

    // just get the same token pair
    List<WrappedByteArray> snapshotList = new ArrayList<>();
    if (!collectionList.isEmpty()) {
      snapshotList = collectionList.keySet().stream()
          .filter(e -> MarketUtils.pairKeyIsEqual(e.getBytes(), key))
          .collect(Collectors.toList());
    }

    // for delete operation
    long limitLevelDB = limit + collectionList.size();

    List<WrappedByteArray> levelDBList = new ArrayList<>();
    if (((SnapshotRoot) head.getRoot()).db.getClass() == LevelDB.class) {
      ((LevelDB) ((SnapshotRoot) head.getRoot()).db).getDb().getKeysNext(key, limitLevelDB)
          .forEach(e -> levelDBList.add(WrappedByteArray.of(e)));
    } else if (((SnapshotRoot) head.getRoot()).db.getClass() == RocksDB.class) {
      ((RocksDB) ((SnapshotRoot) head.getRoot()).db).getDb().getKeysNext(key, limitLevelDB)
          .forEach(e -> levelDBList.add(WrappedByteArray.of(e)));
    }

    // just get the same token pair
    List<WrappedByteArray> levelDBListFiltered = levelDBList.stream()
        .filter(e -> MarketUtils.pairKeyIsEqual(e.getBytes(), key))
        .collect(Collectors.toList());

    List<WrappedByteArray> keyList = new ArrayList<>();
    keyList.addAll(levelDBListFiltered);

    // snapshot and levelDB will have duplicated key, so need to check it before,
    // and remove the key which has been deleted
    snapshotList.forEach(ssKey -> {
      if (!keyList.contains(ssKey)) {
        keyList.add(ssKey);
      }
      if (collectionList.get(ssKey) == Operator.DELETE) {
        keyList.remove(ssKey);
      }
    });

    return keyList.stream()
        .filter(e -> MarketUtils.greaterOrEquals(e.getBytes(), key))
        .sorted((e1, e2) -> MarketUtils.comparePriceKey(e1.getBytes(), e2.getBytes()))
        .limit(limit)
        .map(WrappedByteArray::getBytes)
        .collect(Collectors.toList());
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L152-159)
```java
    } catch (ItemNotFoundException
        | InvalidProtocolBufferException
        | BalanceInsufficientException
        | ContractValidateException e) {
      logger.debug(e.getMessage(), e);
      ret.setStatus(fee, code.FAILED);
      throw new ContractExeException(e.getMessage());
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L307-326)
```java
  private void matchOrder(MarketOrderCapsule takerCapsule, MarketPrice takerPrice,
      TransactionResultCapsule ret, AccountCapsule takerAccountCapsule)
      throws ItemNotFoundException, ContractValidateException {

    byte[] makerSellTokenID = buyTokenID;
    byte[] makerBuyTokenID = sellTokenID;
    byte[] makerPair = MarketUtils.createPairKey(makerSellTokenID, makerBuyTokenID);

    // makerPair not exists
    long makerPriceNumber = pairToPriceStore.getPriceNum(makerPair);
    if (makerPriceNumber == 0) {
      return;
    }
    long remainCount = makerPriceNumber;

    // get maker price list
    List<byte[]> priceKeysList = pairPriceToOrderStore
        .getPriceKeysList(MarketUtils.getPairPriceHeadKey(makerSellTokenID, makerBuyTokenID),
            (long) (MAX_MATCH_NUM + 1), makerPriceNumber, true);

```

**File:** framework/src/main/java/org/tron/core/Wallet.java (L2850-2881)
```java
  public MarketPriceList getMarketPriceByPair(byte[] sellTokenId, byte[] buyTokenId)
      throws BadItemException {
    MarketUtils.checkPairValid(sellTokenId, buyTokenId);

    MarketPairToPriceStore marketPairToPriceStore = dbManager.getChainBaseManager()
        .getMarketPairToPriceStore();
    MarketPairPriceToOrderStore marketPairPriceToOrderStore = dbManager.getChainBaseManager()
        .getMarketPairPriceToOrderStore();

    MarketPriceList.Builder marketPriceListBuilder = MarketPriceList.newBuilder()
        .setSellTokenId(ByteString.copyFrom(sellTokenId))
        .setBuyTokenId(ByteString.copyFrom(buyTokenId));

    long count = marketPairToPriceStore.getPriceNum(sellTokenId, buyTokenId);
    if (count == 0) {
      return marketPriceListBuilder.build();
    }

    long limit = count < MARKET_COUNT_LIMIT_MAX ? count : MARKET_COUNT_LIMIT_MAX;

    List<byte[]> priceKeysList = marketPairPriceToOrderStore
        .getPriceKeysList(sellTokenId, buyTokenId, limit);

    priceKeysList.forEach(
        priceKey -> {
          MarketPrice marketPrice = MarketUtils.decodeKeyToMarketPrice(priceKey);
          marketPriceListBuilder.addPrices(marketPrice);
        }
    );

    return marketPriceListBuilder.build();
  }
```

**File:** framework/src/main/java/org/tron/core/services/http/GetMarketPriceByPairServlet.java (L58-66)
```java
  private void fillResponse(boolean visible, byte[] sellTokenId, byte[] buyTokenId,
      HttpServletResponse response) throws Exception {
    MarketPriceList reply = wallet.getMarketPriceByPair(sellTokenId, buyTokenId);
    if (reply != null) {
      response.getWriter().println(JsonFormat.printToString(reply, visible));
    } else {
      response.getWriter().println("{}");
    }
  }
```
