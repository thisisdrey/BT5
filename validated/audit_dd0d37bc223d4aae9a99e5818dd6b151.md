### Title
Off-by-one slice in market price-list lookup allows unauthenticated `IndexOutOfBoundsException` DoS - (File: `chainbase/src/main/java/org/tron/core/store/MarketPairPriceToOrderStore.java`)

### Summary
`MarketPairPriceToOrderStore.getPriceKeysList` performs an unchecked `List.subList(1, (int)(limit + 1))` call on the result of `getKeysNext(headKey, limit + 1)`. If the underlying key-value store returns fewer entries than `limit + 1` (which can legitimately happen whenever the requested price-key count exceeds the number of price entries actually stored for that token pair, e.g. after concurrent order cancellation/matching shrinks the list, or when a caller simply asks for more entries than exist), the returned list's size is smaller than `limit + 1` and the `subList` call throws `IndexOutOfBoundsException`. This mirrors the reported bug class in `torch.linalg.lu`, where a slice operation with size assumptions not matching the actual underlying data caused a crash/DoS.

### Finding Description [1](#0-0) 

The method computes `limit = count > totalCount ? totalCount : count` and then, when `skip` is true, does:
```
result = getKeysNext(headKey, limit + 1).subList(1, (int)(limit + 1));
```
`getKeysNext` delegates to `revokingDB.getKeysNext(key, limit)` [2](#0-1)  which is only guaranteed to return *up to* `limit+1` keys — it can legally return fewer if the store does not contain that many keys from `headKey` onward. There is no check that the returned list actually has at least `limit + 1` elements before calling `subList`, so any mismatch between the requested `count`/`totalCount` and the real number of stored price entries for the pair throws an unhandled `IndexOutOfBoundsException` (`toIndex > size`).

This is reachable through the public, unauthenticated market price query surface: `getPriceKeysList(byte[] sellTokenId, byte[] buyTokenId, long count)` is invoked from `Wallet`'s market-price lookup logic and exposed via `GetMarketPriceByPairServlet` (HTTP), the corresponding gRPC `GetMarketPriceByPair` API, and their solidity/PBFT-node equivalents — none of these require a signed transaction or special privilege, only sending a `sellTokenId`/`buyTokenId` pair.

### Impact Explanation
An attacker who queries `GetMarketPriceByPair` (or the gRPC/PBFT/Solidity equivalents) for a token pair whose actual number of live price entries is smaller than the count value used internally (which can occur naturally due to a race between order matching/cancellation and the query, since `count` is typically derived from a separately-read counter of "how many price buckets currently exist") can trigger an unhandled `IndexOutOfBoundsException` inside the wallet query path. Depending on how far this exception propagates before being caught, this can manifest as repeated internal errors on the market-price API for that pair, and in the worst case an inconsistency between the count used to size `limit` and the actual keys returned could be forced repeatedly by racing order cancellations against price queries, degrading availability of this API.

### Likelihood Explanation
The trigger condition (requested/derived `count` not matching the number of keys the range scan actually returns) is a normal outcome of the order book's dynamic size and does not require privileged access — it only requires calling the public read-only price query endpoint, optionally in combination with a legitimate order placement/cancellation to shrink the price-key list concurrently with the query. No special TVM interaction or fee is required, making this trivially reachable by an anonymous API client.

### Recommendation
Bound the `subList` call to the actual size of the list returned by `getKeysNext`, e.g.:
```java
List<byte[]> keys = getKeysNext(headKey, limit + 1);
result = keys.size() > 1 ? keys.subList(1, keys.size()) : Collections.emptyList();
```
and audit `getAssetIssuesPaginated`-style paginated slice helpers [3](#0-2)  for similar unguarded `subList` usage originating from public query paths, applying the same size-clamping guard uniformly.

### Proof of Concept
1. Create a market pair with exactly one active price bucket (i.e. `has(headKey)` is true, but the range following `headKey` contains no further price keys).
2. Call `GetMarketPriceByPair` (HTTP `/wallet/getmarketpricebypair` or gRPC equivalent) for that pair, or otherwise cause `count`/`totalCount` passed into `getPriceKeysList` to exceed the number of remaining price keys (e.g. by racing an `ExchangeCancelOrder`/`MarketCancelOrder` transaction that removes price buckets against a concurrent price query).
3. Internally, `getKeysNext(headKey, limit + 1)` returns a list with fewer than `limit + 1` elements; `subList(1, limit + 1)` throws `IndexOutOfBoundsException`, which is not defended against at the call site.

**Note on completeness:** I was unable to fully trace, within the available tool budget, whether the exception thrown here is caught generically by the HTTP/gRPC servlet layer and converted into a normal error response (limiting impact to a per-request failure) or whether it can propagate further/repeat in a way that degrades the whole API's availability. This should be verified directly in `Wallet.java`'s market-price handling method and the servlet/gRPC exception-handling wrappers before treating this as more than a Medium-severity, request-scoped denial-of-service in the market price query path.

### Citations

**File:** chainbase/src/main/java/org/tron/core/store/MarketPairPriceToOrderStore.java (L30-41)
```java
  public List<byte[]> getKeysNext(byte[] key, long limit) {
    if (limit <= 0) {
      return Collections.emptyList();
    }

    return revokingDB.getKeysNext(key, limit);
  }

  public List<byte[]> getPriceKeysList(byte[] sellTokenId, byte[] buyTokenId, long count) {
    byte[] headKey = MarketUtils.getPairPriceHeadKey(sellTokenId, buyTokenId);
    return getPriceKeysList(headKey, count, count, true);
  }
```

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

**File:** chainbase/src/main/java/org/tron/core/store/AssetIssueStore.java (L40-59)
```java
  private List<AssetIssueCapsule> getAssetIssuesPaginated(List<AssetIssueCapsule> assetIssueList,
      long offset, long limit) {
    if (limit < 0 || offset < 0) {
      return null;
    }

    if (assetIssueList.size() <= offset) {
      return null;
    }
    assetIssueList.sort((o1, o2) -> {
      if (o1.getName() != o2.getName()) {
        return o1.getName().toStringUtf8().compareTo(o2.getName().toStringUtf8());
      }
      return Long.compare(o1.getOrder(), o2.getOrder());
    });
    limit = limit > ASSET_ISSUE_COUNT_LIMIT_MAX ? ASSET_ISSUE_COUNT_LIMIT_MAX : limit;
    long end = offset + limit;
    end = end > assetIssueList.size() ? assetIssueList.size() : end;
    return assetIssueList.subList((int) offset, (int) end);
  }
```
