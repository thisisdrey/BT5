## Analysis Result

Found a valid analog: an unbounded per-request loop in the market order query path, unlike its sibling market-query methods which all cap iteration at `MARKET_COUNT_LIMIT_MAX`.

### Title
Unbounded iteration in `getMarketOrderByAccount` allows a single account with many open orders to cause a denial-of-service via a single unauthenticated query - (File: framework/src/main/java/org/tron/core/Wallet.java)

### Summary
`Wallet.getMarketOrderByAccount(ByteString accountAddress)` iterates over the *entire* order-id list stored in a `MarketAccountOrderCapsule` for the given address, fetching and serializing every single order into the response with no size cap. [1](#0-0)  This is reachable from unauthenticated HTTP (`GetMarketOrderByAccountServlet`) and gRPC (`RpcApiService.getMarketOrderByAccount`) endpoints with only an address as input. [2](#0-1) [3](#0-2) 

### Finding Description
Every other market-listing API in `Wallet.java` enforces a hard cap of `MARKET_COUNT_LIMIT_MAX` results per response: `getMarketPriceByPair` clamps `limit` to `MARKET_COUNT_LIMIT_MAX`, `getMarketPairList` breaks the iterator once `count > MARKET_COUNT_LIMIT_MAX`, and `getMarketOrderListByPair` breaks once `MARKET_COUNT_LIMIT_MAX - countForOrder <= 0`. [4](#0-3) 

`getMarketOrderByAccount`, however, has no such bound — it calls `.forEach` over the *full* `orderIdList` returned by `marketAccountOrderCapsule.getOrdersList()`, performing a store lookup (`marketOrderStore.get(...)`) and full protobuf serialization for every entry: [5](#0-4) 

The underlying capsule, `MarketAccountOrderCapsule.addOrders`, unconditionally appends to the repeated `orders` field with no size check at write time either: [6](#0-5) 

`MARKET_COUNT_LIMIT_MAX` is referenced only in `Wallet.java` (the query-side caps) and not in `MarketSellAssetActuator`, meaning order creation is not itself capped by this constant — an account's open-order count can grow arbitrarily large through repeated cheap `MarketSellAssetContract` broadcasts, each of which only needs to clear normal bandwidth/energy costs, not a market-specific ceiling.

This mirrors the CVE-2021-20326 bug class: a normally-authorized, low-cost user action (placing many small orders — analogous to inserting many small documents) followed by a single crafted read query that the server processes without any result-size bound, consuming disproportionate CPU/memory/serialization time on the node servicing the query.

### Impact Explanation
An attacker who accumulates a very large number of open orders on one address can cause any node (including public/gRPC/HTTP-facing full nodes and solidity/PBFT nodes, since `getMarketOrderByAccount` is also proxied by `RpcApiServiceOnSolidity`/`RpcApiServiceOnPBFT`) to spend excessive time and memory building the full response when *any* third party queries that address's orders. [7](#0-6)  Because the endpoint is unauthenticated and takes only an address, repeated invocation by any client can be used to keep pinning node resources, degrading or denying that API path for other users — consistent with the "no impact beyond node/API DoS" bar required by the validation rules.

### Likelihood Explanation
Medium. Growing the order list requires broadcasting many `MarketSellAssetContract` transactions from the attacker's own account (self-funded, no privilege needed), each subject to normal bandwidth/energy costs but with no market-specific per-account limit blocking it. Once the list is large, every subsequent unauthenticated query against that address is expensive and repeatable at will by any anonymous client, so the DoS trigger itself is trivial and cheap relative to the cost imposed on the queried node.

### Recommendation
Apply the same `MARKET_COUNT_LIMIT_MAX` bound already used in `getMarketPriceByPair`/`getMarketPairList`/`getMarketOrderListByPair` to `getMarketOrderByAccount` — either cap the number of orders serialized/looked-up per call (with pagination), or enforce a maximum number of concurrent open orders per account in `MarketSellAssetActuator` at order-creation time.

### Proof of Concept
1. From a funded account, repeatedly broadcast `MarketSellAssetContract` transactions to accumulate a very large number of open orders under one address (no actuator-side cap prevents this).
2. From any client (unauthenticated), call `GET /wallet/getmarketorderbyaccount?value=<hex address>` or the gRPC `GetMarketOrderByAccount` RPC with that address.
3. Observe that `Wallet.getMarketOrderByAccount` performs one store `get` plus full protobuf construction per order with no cap, and the response includes every order — repeated calls from any caller consume disproportionate node CPU/memory relative to normal, bounded market queries.

### Citations

**File:** framework/src/main/java/org/tron/core/Wallet.java (L2811-2848)
```java
  public MarketOrderList getMarketOrderByAccount(ByteString accountAddress) {

    if (accountAddress == null || accountAddress.isEmpty()) {
      return null;
    }

    MarketAccountOrderCapsule marketAccountOrderCapsule;
    try {
      marketAccountOrderCapsule = dbManager.getChainBaseManager()
          .getMarketAccountStore().get(accountAddress.toByteArray());
    } catch (ItemNotFoundException e) {
      return null;
    }

    MarketOrderStore marketOrderStore = dbManager.getChainBaseManager().getMarketOrderStore();

    MarketOrderList.Builder marketOrderListBuilder = MarketOrderList.newBuilder();
    List<ByteString> orderIdList = marketAccountOrderCapsule.getOrdersList();

    orderIdList.forEach(
        orderId -> {
          try {
            MarketOrderCapsule orderCapsule = marketOrderStore.get(orderId.toByteArray());
            // set prev and next, hide these messages in the print
            orderCapsule.setPrev(new byte[0]);
            orderCapsule.setNext(new byte[0]);

            marketOrderListBuilder
                .addOrders(orderCapsule.getInstance());
          } catch (ItemNotFoundException e) {
            logger.warn("orderId = {} not found", orderId);
            throw new IllegalStateException("order not found in store");
          }
        }
    );

    return marketOrderListBuilder.build();
  }
```

**File:** framework/src/main/java/org/tron/core/Wallet.java (L2863-2952)
```java
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

  public MarketOrderList getMarketOrderListByPair(byte[] sellTokenId, byte[] buyTokenId)
      throws ItemNotFoundException, BadItemException {
    MarketUtils.checkPairValid(sellTokenId, buyTokenId);

    MarketOrderList.Builder builder = MarketOrderList.newBuilder();

    MarketPairToPriceStore marketPairToPriceStore = dbManager.getChainBaseManager()
        .getMarketPairToPriceStore();
    MarketPairPriceToOrderStore marketPairPriceToOrderStore = dbManager.getChainBaseManager()
        .getMarketPairPriceToOrderStore();
    MarketPairPriceToOrderStore pairPriceToOrderStore = dbManager.getChainBaseManager()
        .getMarketPairPriceToOrderStore();
    MarketOrderStore orderStore = dbManager.getChainBaseManager().getMarketOrderStore();

    long countForPrice = marketPairToPriceStore.getPriceNum(sellTokenId, buyTokenId);
    if (countForPrice == 0) {
      return builder.build();
    }
    long limitForPrice =
        countForPrice < MARKET_COUNT_LIMIT_MAX ? countForPrice : MARKET_COUNT_LIMIT_MAX;

    List<byte[]> priceKeysList = marketPairPriceToOrderStore
        .getPriceKeysList(sellTokenId, buyTokenId, limitForPrice);

    long countForOrder = 0;
    for (byte[] pairPriceKey : priceKeysList) {
      MarketOrderIdListCapsule orderIdListCapsule = pairPriceToOrderStore
          .getUnchecked(pairPriceKey);
      if (MARKET_COUNT_LIMIT_MAX - countForOrder <= 0) {
        break;
      }
      if (orderIdListCapsule != null) {
        List<MarketOrderCapsule> orderList = orderIdListCapsule
            .getAllOrder(orderStore, MARKET_COUNT_LIMIT_MAX - countForOrder);

        orderList.forEach(orderCapsule -> {
          // set prev and next, hide these messages in the print
          orderCapsule.setPrev(new byte[0]);
          orderCapsule.setNext(new byte[0]);

          builder.addOrders(orderCapsule.getInstance());
        });
        countForOrder += orderList.size();
      }
    }

    return builder.build();
  }
```

**File:** framework/src/main/java/org/tron/core/services/http/GetMarketOrderByAccountServlet.java (L17-37)
```java
@Component
@Slf4j(topic = "API")
public class GetMarketOrderByAccountServlet extends RateLimiterServlet {

  @Autowired
  private Wallet wallet;

  protected void getResult(String address, boolean visible, HttpServletResponse response)
      throws IOException {
    if (visible) {
      address = Util.getHexAddress(address);
    }

    MarketOrderList reply = wallet
        .getMarketOrderByAccount(ByteString.copyFrom(ByteArray.fromHexString(address)));
    if (reply != null) {
      response.getWriter().println(JsonFormat.printToString(reply, visible));
    } else {
      response.getWriter().println("{}");
    }
  }
```

**File:** framework/src/main/java/org/tron/core/services/RpcApiService.java (L2560-2573)
```java
    @Override
    public void getMarketOrderByAccount(BytesMessage request,
        StreamObserver<MarketOrderList> responseObserver) {
      try {
        ByteString address = request.getValue();

        MarketOrderList marketOrderList = wallet
            .getMarketOrderByAccount(address);
        responseObserver.onNext(marketOrderList);
      } catch (Exception e) {
        responseObserver.onError(getRunTimeException(e));
      }
      responseObserver.onCompleted();
    }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/MarketAccountOrderCapsule.java (L55-60)
```java
  public void addOrders(ByteString order) {
    this.accountOrder = this.accountOrder.toBuilder()
        .addOrders(order)
        .build();

  }
```

**File:** framework/src/main/java/org/tron/core/services/interfaceOnSolidity/RpcApiServiceOnSolidity.java (L414-421)
```java
    @Override
    public void getMarketOrderByAccount(BytesMessage request,
        StreamObserver<MarketOrderList> responseObserver) {
      walletOnSolidity.futureGet(
          () -> rpcApiService.getWalletSolidityApi()
              .getMarketOrderByAccount(request, responseObserver)
      );
    }
```
