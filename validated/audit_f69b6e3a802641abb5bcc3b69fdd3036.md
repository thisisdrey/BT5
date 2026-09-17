### Title
Unbounded per-account market order list causes resource-exhaustion DoS in `Wallet.getMarketOrderByAccount` - (File: framework/src/main/java/org/tron/core/Wallet.java)

### Summary
`Wallet.getMarketOrderByAccount` iterates, without any limit or pagination, over the full list of order IDs an account has ever created via `MarketSellAssetContract`, performing a store lookup for every entry to build the gRPC/HTTP response. The list that backs this iteration, `MarketAccountOrderCapsule.getOrdersList()`, is only ever appended to and is never pruned when an order is matched or cancelled, so it grows without bound as a single account keeps trading. This is the same bug class as the reported `CvgERC721TimeLockingUpgradeable.getTokenIdsForWallet` issue: an unbounded loop over a per-account collection, reachable through an unauthenticated query endpoint, whose size is driven by normal but repeatable user activity.

### Finding Description
`MarketSellAssetActuator.createAndSaveOrder` always calls `marketAccountOrderCapsule.addOrders(orderCapsule.getID())` for every new sell order: [1](#0-0) 

The actuator only caps the number of *active* orders via `MAX_ACTIVE_ORDER_NUM = 100`, checked against `marketAccountOrderCapsule.getCount()`: [2](#0-1) 

`count` (active orders) is decremented when an order becomes inactive via `MarketUtils.updateOrderState`, both on full/partial match (`matchSingleOrder`) and on cancellation (`MarketCancelOrderActuator.execute`, which calls `orderIdListCapsule.removeOrder(...)` only on the *pair-price* order-book linked list, not on the per-account `MarketAccountOrderCapsule`): [3](#0-2) 

Crucially, `MarketAccountOrderCapsule.removeOrder` (which would shrink `getOrdersList()`) exists but is never invoked by either `MarketSellAssetActuator` or `MarketCancelOrderActuator`: [4](#0-3) 

Because active-order capacity frees up as soon as an order is matched or cancelled, an account can keep re-using its 100 active-order slots indefinitely while `getOrdersList()` keeps growing forever (bounded only by the market-sell/cancel transaction fees the attacker is willing to pay, which are small, fixed fees, not scaled to the growing list size).

`Wallet.getMarketOrderByAccount` then reads this unbounded list and does a per-order DB lookup and response-object build for every single entry, with no offset/limit parameters: [5](#0-4) 

This method is exposed with no pagination on every wallet API surface: plain gRPC `Wallet` service, [6](#0-5) 
the Solidity/PBFT gRPC mirrors, [7](#0-6) 
and the unauthenticated HTTP endpoint `/wallet/getmarketorderbyaccount` (plus its `walletsolidity`/`walletpbft` mirrors): [8](#0-7) 

### Impact Explanation
Any external, unauthenticated caller can request `getMarketOrderByAccount` for an address whose owner has accumulated a very large historical order list. Each such request forces the node to perform one RocksDB lookup and protobuf object construction per historical order (potentially tens of thousands, growing without bound over time), for every incoming request, with no caching or pagination. Because this endpoint is reachable via HTTP, gRPC, and their Solidity/PBFT proxies on every full node/witness node, a small number of concurrent requests against a "poisoned" address can consume disproportionate CPU/memory/IO on the serving node's API thread pool, degrading or denying the JSON-RPC/HTTP/gRPC query service for legitimate users — matching the "an API the node can no longer serve" acceptance criterion.

### Likelihood Explanation
The attack requires only:
1. Repeated `MarketSellAssetContract` transactions from a single address, each paying only the fixed `MarketSellFee`, immediately matched or cancelled (freeing the 100-slot active cap) to allow unbounded repetition.
2. A subsequent unauthenticated call to `getMarketOrderByAccount` (or its solidity/PBFT/HTTP variants) targeting that address.

No special privileges, admin actions, or malicious-node behavior are required — any transaction broadcaster and any anonymous API caller can trigger this. The per-transaction cost is a fixed, low fee, so the growth of the unbounded list is entirely within the reach of a single unprivileged actor over time.

### Recommendation
- Add pagination (offset/limit) parameters to `Wallet.getMarketOrderByAccount` and its API surfaces, capping the maximum number of orders returned/looked-up per call, similar to `getAssetIssueList(offset, limit)` / `getPaginatedProposalList`.
- Alternatively (or additionally), prune `MarketAccountOrderCapsule.getOrdersList()` by calling `removeOrder` when an order becomes inactive (matched/cancelled) in `MarketSellAssetActuator`/`MarketCancelOrderActuator`, or maintain a separate bounded "active orders" list distinct from historical order IDs.
- Enforce a hard cap on the total (not just active) number of orders returned in a single `MarketOrderList` response.

### Proof of Concept
1. Attacker creates account A and repeatedly submits `MarketSellAssetContract` transactions that are designed to fully match immediately (or subsequently cancels them with `MarketCancelOrderContract`), each paying only the fixed sell/cancel fee. Because `count` (active orders) drops back down after each match/cancel, account A can repeat this indefinitely, e.g., tens of thousands of times, while `MarketAccountOrderCapsule.getOrdersList()` for A keeps growing (verified: neither actuator ever calls `MarketAccountOrderCapsule.removeOrder`).
2. Any anonymous client sends `GET/POST /wallet/getmarketorderbyaccount` (or the gRPC `getMarketOrderByAccount` call) with `address = A`.
3. `Wallet.getMarketOrderByAccount` loads the full historical `orderIdList` and performs an `orderStore.get()` DB read plus protobuf `addOrders()` build for every entry in a single request thread, with no size limit, causing significant CPU/memory/time cost proportional to A's lifetime order count — repeatable and amplifiable by concurrent requests against the same poisoned address.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L232-239)
```java
    // check order num
    MarketAccountOrderCapsule marketAccountOrderCapsule = marketAccountStore
        .getUnchecked(ownerAddress);
    if (marketAccountOrderCapsule != null
        && marketAccountOrderCapsule.getCount() >= MAX_ACTIVE_ORDER_NUM) {
      throw new ContractValidateException(
          "Maximum number of orders exceeded，" + MAX_ACTIVE_ORDER_NUM);
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L501-522)
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
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketCancelOrderActuator.java (L103-121)
```java
      // 1. return balance and token
      MarketUtils
          .returnSellTokenRemain(orderCapsule, accountCapsule, dynamicStore, assetIssueStore);

      MarketUtils.updateOrderState(orderCapsule, State.CANCELED, marketAccountStore);
      accountStore.put(orderCapsule.getOwnerAddress().toByteArray(), accountCapsule);
      orderStore.put(orderCapsule.getID().toByteArray(), orderCapsule);

      // 2. clear orderList
      byte[] pairPriceKey = MarketUtils.createPairPriceKey(
          orderCapsule.getSellTokenId(),
          orderCapsule.getBuyTokenId(),
          orderCapsule.getSellTokenQuantity(),
          orderCapsule.getBuyTokenQuantity()
      );
      MarketOrderIdListCapsule orderIdListCapsule = pairPriceToOrderStore.get(pairPriceKey);

      // delete order
      orderIdListCapsule.removeOrder(orderCapsule, orderStore, pairPriceKey, pairPriceToOrderStore);
```

**File:** chainbase/src/main/java/org/tron/core/capsule/MarketAccountOrderCapsule.java (L62-75)
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
