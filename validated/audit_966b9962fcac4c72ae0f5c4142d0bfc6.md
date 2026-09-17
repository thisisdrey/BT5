Found a strong analog: `MarketAccountOrderCapsule.getOrdersList()` — an unbound per-account order-id list that only grows (never trimmed on cancel/finish beyond removing the single matched id, and every `MarketSellAssetContract` transaction appends one more `orderId`) — is fully materialized and iterated by `Wallet.getMarketOrderByAccount()`, which is reachable by any anonymous HTTP/gRPC client with no authentication and no pagination/limit.

### Title
Unbounded per-account market order list causes unbounded work in `getMarketOrderByAccount` API - (File: framework/src/main/java/org/tron/core/Wallet.java)

### Summary
`MarketAccountOrderCapsule` stores an ever-growing `orders` list (`repeated bytes orders`) for every account that has ever placed a market order via `MarketSellAssetActuator`. Each `MarketSellAssetContract` transaction appends one entry via `MarketAccountOrderCapsule.addOrders()` [1](#0-0)  and creates a new order entity in `MarketOrderStore` via `createAndSaveOrder` [2](#0-1) . There is no cap on the total number of orders (active or historical) a single account can accumulate — the `MAX_ACTIVE_ORDER_NUM`/`MAX_MATCH_NUM` constants only bound *matching* work per single transaction, not the size of this persisted list [3](#0-2) .

`Wallet.getMarketOrderByAccount()` fetches this full `orders` list and, for every single entry, performs an unconditional `marketOrderStore.get()` DB read and appends the full order object to the response, with no limit, pagination, or cap on list size: [4](#0-3) 

This method is exposed directly to unauthenticated callers through multiple public entry points with no size limiting:
- HTTP `/wallet/getmarketorderbyaccount` via `GetMarketOrderByAccountServlet.getResult()` [5](#0-4) 
- gRPC `GetMarketOrderByAccount` via `RpcApiService` [6](#0-5) 
- and the equivalent Solidity/PBFT read-only mirrors.

### Impact Explanation
A user can cheaply place many `MarketSellAssetContract` transactions from a single account (each is a normal signed transaction, gas/fee-bounded per tx but with no global per-account cap), causing that account's `MarketAccountOrderCapsule.orders` list to grow without bound over time. Once the list is large enough, a single unauthenticated call to `getMarketOrderByAccount` for that address forces the node to perform O(n) DB reads and build/serialize an O(n)-sized response in one synchronous RPC/HTTP handler thread. This is a targeted resource-exhaustion vector against the node's public query API — an attacker can grow their own account's list arbitrarily (self-targeted, but publicly queryable by anyone, including automated indexers/exchanges), degrading or blocking that query path and consuming node CPU/memory/DB I/O on every call, which can starve other RPC/API threads on the same node.

### Likelihood Explanation
Likelihood is moderate-to-high: creating market orders only requires an account with a small asset/TRX balance and does not require any privileged role (any unprivileged transaction broadcaster can call `MarketSellAssetContract`). Each order creation costs the `getMarketSellFee()` fee [7](#0-6) , and the historical `orders` list is never pruned when orders are matched/canceled (only the active `count` is decremented in `removeOrder`, while `total_count`/order id list only grows) [8](#0-7) . The exposed query API is unauthenticated and directly reachable, matching the report's "cheap unbound growth + unbound loop in reachable query path" pattern exactly, mirroring the original `deposits`/`getLockedFunds` DoS class.

### Recommendation
- Cap the size of `MarketAccountOrderCapsule.orders` (e.g., only retain active orders, or a bounded rolling window of recent historical order ids), and/or move historical order lookups off the hot per-account capsule into a paginated index.
- Add pagination/limit parameters to `getMarketOrderByAccount` (and its HTTP/gRPC wrappers) so a single call cannot force unbounded DB reads and unbounded response construction.
- Consider charging escalating fees or enforcing a maximum outstanding+historical order count per account in `MarketSellAssetActuator.validate()`.

### Proof of Concept
1. From an unprivileged account with a small TRX/asset balance, repeatedly broadcast `MarketSellAssetContract` transactions (each pays `getMarketSellFee()`), causing `createAndSaveOrder` to append to `MarketAccountOrderCapsule.orders` each time: [2](#0-1) 
2. Repeat until the `orders` list contains a very large number of entries (list is never trimmed on cancel besides removing the single matched id, see `removeOrder`) [8](#0-7) .
3. Call the public `/wallet/getmarketorderbyaccount` HTTP endpoint (or gRPC `GetMarketOrderByAccount`) with that address as an anonymous caller: [5](#0-4) 
4. Observe that `Wallet.getMarketOrderByAccount` performs one `marketOrderStore.get()` per stored order id and serializes the entire result set in a single synchronous call with no limit, causing disproportionate CPU/IO/memory cost per request: [4](#0-3)

### Citations

**File:** chainbase/src/main/java/org/tron/core/capsule/MarketAccountOrderCapsule.java (L55-60)
```java
  public void addOrders(ByteString order) {
    this.accountOrder = this.accountOrder.toBuilder()
        .addOrders(order)
        .build();

  }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/MarketAccountOrderCapsule.java (L62-74)
```java
  public void removeOrder(ByteString orderId) {
    List<ByteString> orderList = Lists.newArrayList();
    orderList.addAll(this.getOrdersList());
    orderList.remove(orderId);

    this.accountOrder = this.accountOrder.toBuilder()
        .setCount(this.getCount() - 1)
        .clearOrders()
        .addAllOrders(orderList)
        .build();


  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L59-66)
```java
@Slf4j(topic = "actuator")
public class MarketSellAssetActuator extends AbstractActuator {

  @Getter
  @Setter
  private static int MAX_ACTIVE_ORDER_NUM = 100;
  @Getter
  private static int MAX_MATCH_NUM = 20;
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L288-291)
```java
  @Override
  public long calcFee() {
    return dynamicStore.getMarketSellFee();
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L501-525)
```java
  private MarketOrderCapsule createAndSaveOrder(AccountCapsule accountCapsule,
      MarketSellAssetContract contract) {
    MarketAccountOrderCapsule marketAccountOrderCapsule = marketAccountStore
        .getUnchecked(contract.getOwnerAddress().toByteArray());
    if (marketAccountOrderCapsule == null) {
      marketAccountOrderCapsule = new MarketAccountOrderCapsule(contract.getOwnerAddress());
    }

    // note: here use total_count
    byte[] orderId = MarketUtils
        .calculateOrderId(contract.getOwnerAddress(), sellTokenID, buyTokenID,
            marketAccountOrderCapsule.getTotalCount());
    MarketOrderCapsule orderCapsule = new MarketOrderCapsule(orderId, contract);

    long now = dynamicStore.getLatestBlockHeaderTimestamp();
    orderCapsule.setCreateTime(now);

    marketAccountOrderCapsule.addOrders(orderCapsule.getID());
    marketAccountOrderCapsule.setCount(marketAccountOrderCapsule.getCount() + 1);
    marketAccountOrderCapsule.setTotalCount(marketAccountOrderCapsule.getTotalCount() + 1);
    marketAccountStore.put(accountCapsule.createDbKey(), marketAccountOrderCapsule);
    orderStore.put(orderId, orderCapsule);

    return orderCapsule;
  }
```

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

**File:** framework/src/main/java/org/tron/core/services/http/GetMarketOrderByAccountServlet.java (L24-37)
```java
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

**File:** framework/src/main/java/org/tron/core/services/RpcApiService.java (L788-801)
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
