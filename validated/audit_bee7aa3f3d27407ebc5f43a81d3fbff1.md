### Title
Unchecked list-size assumption before `subList` causes node crash on legitimate Market order matching - (File: `chainbase/src/main/java/org/tron/core/store/MarketPairPriceToOrderStore.java`)

### Summary
Analogous to the kernel bug where a pipe index was used to access a fixed-size array without validating that the index is within bounds before use, `MarketPairPriceToOrderStore.getPriceKeysList` computes a `limit` from a caller-supplied `count`/`totalCount` and blindly calls `.subList(1, (int)(limit + 1))` on the result of `getKeysNext(headKey, limit + 1)`, without first checking that the returned list actually contains `limit + 1` elements.

### Finding Description
`getPriceKeysList` at [1](#0-0)  does:
```
long limit = count > totalCount ? totalCount : count;
if (skip) {
  result = getKeysNext(headKey, limit + 1).subList(1, (int)(limit + 1));
}
```
`totalCount`/`count` here originate from `MarketPairToPriceStore`'s stored price-pair counter (`makerPriceNumber`), which is read and used as the size hint in `MarketSellAssetActuator.matchOrder`: [2](#0-1) 

The underlying assumption is that `pairToPriceStore`'s counter for a given (sellToken, buyToken) pair always matches the number of keys actually retrievable from `getKeysNext` on the underlying revoking key-value store for that head key. If these two pieces of state ever diverge — e.g., due to revoking-DB layering/flush timing, cache versus underlying store inconsistency, or any code path that updates one store without atomically updating the other in the exact same commit boundary — `getKeysNext(headKey, limit + 1)` can return fewer than `limit + 1` elements, and the subsequent `subList(1, limit + 1)` throws `IndexOutOfBoundsException`. Because this executes inside actuator `execute()` during block application (`MarketSellAssetActuator.execute -> matchOrder -> hasMatch/getPriceKeysList`), an uncaught `IndexOutOfBoundsException` is not one of the exception types caught by `execute()`'s catch clause (`ItemNotFoundException | InvalidProtocolBufferException | BalanceInsufficientException | ContractValidateException`), so it propagates as an unchecked `RuntimeException` out of transaction processing.

### Impact Explanation
If reachable, this is a Denial-of-Service (node halt/crash) vector: an uncaught `RuntimeException` thrown while applying a transaction inside `Manager`'s block-application path can crash or desynchronize a node processing the block, matching the "node crash or halt" acceptance criterion. This exactly mirrors the kernel analog's class of bug (using a size/index value to slice/access a collection without confirming the collection actually has that many entries).

### Likelihood Explanation
I was not able to fully verify a concrete transaction sequence that forces `pairToPriceStore`'s price-pair counter to disagree with the actual number of keys under the head key within a single revoking-DB commit window — this requires deeper tracing of `MarketPairToPriceStore` counter updates, `TronStoreWithRevoking`/`Chainbase` layering, and `MarketCancelOrderActuator`/order-removal code paths than I could complete in the available searches. The code pattern itself (index/size trust without bounds validation before `subList`) is real and present, but confirming it is remotely triggerable by an unprivileged `MarketSellAssetContract` sender requires further investigation of the market-order lifecycle (create/match/cancel) to find a state where the counter and stored keys diverge.

### Recommendation
Defensively bound the `subList` call: check `getKeysNext(...).size() >= limit + 1` before slicing (or use `Math.min` against the actual returned size) and treat a mismatch as "no more matches" rather than throwing. More broadly, ensure the price-pair counter in `MarketPairToPriceStore` and the key set in `MarketPairPriceToOrderStore` are updated atomically/consistently on every order creation, match, and cancellation path, similar to how the kernel fix added an explicit bounds check before dereferencing the `pipe_ctx` array by index.

### Proof of Concept
Not fully constructed — I could not confirm within the available investigation a concrete counter/key-store desynchronization sequence reachable from a single or short series of unprivileged `MarketSellAssetContract`/`MarketCancelOrderContract` transactions. This would need to be validated by a deeper trace of `MarketPairToPriceStore` count mutations versus `MarketPairPriceToOrderStore` key mutations across concurrent/overlapping order matches and cancellations (see `MarketSellAssetActuator.matchOrder` lines 307-380 and `MarketCancelOrderActuator`) before this can be escalated from a theoretical code-pattern finding to a proven exploit.

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

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L316-326)
```java
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
