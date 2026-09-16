## Analog Found

### Title
Unsynchronized concurrent walk vs. mutation of the market order doubly-linked list corrupts on-chain order data and can crash/DoS market query APIs - (File: `chainbase/src/main/java/org/tron/core/capsule/MarketOrderIdListCapsule.java`)

### Summary
The upstream Linux fix serializes two code paths that walk a mutable linked list (`nh->f6i_list`) against a third path that unlinks/frees list nodes, because the walkers took no lock while the mutator held one. java-tron has the same shape of bug: `MarketOrderIdListCapsule` implements a doubly-linked list of market orders (head/tail/prev/next) persisted across `MarketOrderStore`/`MarketPairPriceToOrderStore` as multiple independent key-value writes with **no lock at all** protecting the multi-key mutation, while several unauthenticated, no-signature-required HTTP/gRPC query endpoints in `Wallet.java` (`getMarketOrderListByPair`, `getMarketOrderByAccount`, `getAllOrder`, `getOrderSize`) walk the very same list concurrently.

### Finding Description
`MarketOrderIdListCapsule.removeOrder()` unlinks a node from the list by issuing a sequence of independent, non-atomic `marketOrderStore.put()` calls to update the previous node's `next`, the next node's `prev`, and possibly the list's `head`/`tail` pointer in `pairPriceToOrderStore`: [1](#0-0) 

`addOrder()` performs the same kind of multi-step, unsynchronized pointer update when appending a new tail node: [2](#0-1) 

These mutations are triggered by ordinary, unprivileged transactions: `MarketSellAssetActuator.matchOrder()`/`matchSingleOrder()` call `removeOrder()` while matching orders during block application, and `MarketCancelOrderActuator.execute()` calls `removeOrder()` when a user cancels an order: [3](#0-2) [4](#0-3) 

Meanwhile, `Wallet.getMarketOrderListByPair()` and `Wallet.getMarketOrderByAccount()` — reachable from unauthenticated HTTP servlets (`GetMarketOrderListByPairServlet`, `GetMarketOrderByAccountServlet`) and gRPC (`RpcApiService`) — walk the same list via `MarketOrderIdListCapsule.getAllOrder()`, which follows `head -> getNextCapsule() -> ...` chains directly against the live store, with no lock taken: [5](#0-4) [6](#0-5) 

Unlike RTNL in the kernel, nothing serializes API/query reads against block-application actuator writes in java-tron: the market list mutation is split across several separate store `put()` calls with no `synchronized`/lock wrapping the whole pointer update, and the query path performs no equivalent locking either. A reader thread interleaved between the individual `put()` calls of `removeOrder()`/`addOrder()` can observe a torn, half-updated linked list — e.g., `head` pointing to a node whose `prev`/`next` fields were already changed by the writer, or a `next` pointer to a node whose reciprocal `prev` pointer has not yet been updated. This is the direct analog of the kernel's UAF: an unlocked walk of a list concurrently unlinked by another path, just manifesting in Java as list-pointer corruption/`ItemNotFoundException`/infinite loop instead of a native use-after-free.

### Impact Explanation
A reader that traverses a torn list can: (a) throw an unhandled `ItemNotFoundException`/`IllegalStateException` up through `Wallet` into the HTTP/gRPC handler, making that market-query API endpoint unusable or causing repeated request failures (denial of service for the affected RPC/HTTP path, matching the "API the node can no longer serve" acceptance criterion); or (b) loop indefinitely if pointers form a cycle due to a half-completed unlink, tying up a query-handling thread. Because `MarketSellAssetActuator` and `MarketCancelOrderActuator` are triggered by ordinary, unauthenticated broadcast transactions (`MarketSellAssetContract`, `MarketCancelOrderContract`), any external user can repeatedly submit sell/cancel transactions to keep the list mutating while simultaneously hammering the market query endpoints to reliably trigger the race.

### Likelihood Explanation
Any account can create/cancel market orders via ordinary signed transactions and any client can call the market query HTTP/gRPC endpoints without authentication, so both sides of the race are fully attacker-controlled and require only network access plus a funded account able to place trivial orders — no privileged role, key leakage, or peer compromise is required.

### Recommendation
Protect the multi-key pointer updates in `MarketOrderIdListCapsule.addOrder()`/`removeOrder()` and the corresponding query traversal in `Wallet.getMarketOrderListByPair()`/`getMarketOrderByAccount()`/`getAllOrder()` with a shared lock (or perform the pointer updates atomically via a single write batch) so that readers never observe a partially-linked list, and ensure exceptions from a torn read are caught and returned as a well-formed API error instead of propagating as an unhandled runtime exception.

### Proof of Concept
1. Fund two accounts; have account A create several resting market orders for the same trading pair so the `MarketOrderIdListCapsule` has multiple entries (`addOrder` executed under normal block application).
2. Concurrently: (a) submit `MarketCancelOrderContract`/matching `MarketSellAssetContract` transactions from account A/B in a tight loop to keep triggering `removeOrder()`/`addOrder()` pointer updates on the shared list, and (b) hammer `GetMarketOrderListByPairServlet`/`GetMarketOrderByAccountServlet` (or the gRPC equivalents) in a tight loop from an unauthenticated client.
3. Observe intermittent `ItemNotFoundException`/`IllegalStateException` thrown from `Wallet.getMarketOrderListByPair`/`getMarketOrderByAccount` (propagated as `IllegalStateException("order not found in store")`) or hangs during traversal, demonstrating the unsynchronized read-vs-write race on the linked list.

### Citations

**File:** chainbase/src/main/java/org/tron/core/capsule/MarketOrderIdListCapsule.java (L78-133)
```java
  public void removeOrder(MarketOrderCapsule currentCapsule, MarketOrderStore marketOrderStore,
      byte[] pairPriceKey, MarketPairPriceToOrderStore pairPriceToOrderStore)
      throws ItemNotFoundException {
    MarketOrderCapsule preCapsule = currentCapsule.getPrevCapsule(marketOrderStore);
    MarketOrderCapsule nextCapsule = currentCapsule.getNextCapsule(marketOrderStore);

    // pre.next = current.next
    // current.next.prev = current.prev
    if (preCapsule != null) {
      if (nextCapsule != null) {
        preCapsule.setNext(currentCapsule.getNext());
      } else {
        preCapsule.setNext(new byte[0]);
      }

      marketOrderStore.put(preCapsule.getID().toByteArray(), preCapsule);
    } else {
      // current is head
      // head = current.next
      if (nextCapsule != null) {
        this.setHead(currentCapsule.getNext());
      } else {
        // need to delete, outside
        this.setHead(new byte[0]);
      }

      // head changed
      pairPriceToOrderStore.put(pairPriceKey, this);
    }

    if (nextCapsule != null) {
      if (preCapsule != null) {
        nextCapsule.setPrev(currentCapsule.getPrev());
      } else {
        nextCapsule.setPrev(new byte[0]);
      }

      marketOrderStore.put(nextCapsule.getID().toByteArray(), nextCapsule);
    } else {
      // current is tail
      // this.tail = pre
      if (preCapsule != null) {
        this.setTail(currentCapsule.getPrev());
      } else {
        this.setTail(new byte[0]);
      }

      // tail changed
      pairPriceToOrderStore.put(pairPriceKey, this);
    }

    // update current
    currentCapsule.setPrev(new byte[0]);
    currentCapsule.setNext(new byte[0]);
    marketOrderStore.put(currentCapsule.getID().toByteArray(), currentCapsule);
  }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/MarketOrderIdListCapsule.java (L159-181)
```java
  // add order to linked list
  public void addOrder(MarketOrderCapsule currentCapsule, MarketOrderStore orderStore)
      throws ItemNotFoundException {
    byte[] orderId = currentCapsule.getID().toByteArray();

    if (this.isOrderEmpty()) {
      this.setHead(orderId);
      this.setTail(orderId);
    } else {
      // tail.next = order
      // order.pre = tail
      // this.tail = order
      byte[] tailId = this.getTail();
      MarketOrderCapsule tailCapsule = orderStore.get(tailId);
      tailCapsule.setNext(orderId);
      orderStore.put(tailId, tailCapsule);

      currentCapsule.setPrev(tailId);
      orderStore.put(orderId, currentCapsule);

      this.setTail(orderId);
    }
  }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/MarketOrderIdListCapsule.java (L244-263)
```java
  public List<MarketOrderCapsule> getAllOrder(MarketOrderStore orderStore, long limit)
      throws ItemNotFoundException {

    List<MarketOrderCapsule> result = new ArrayList<>();

    long count = 0;
    byte[] orderId = this.getHead();
    if (!ByteArray.isEmpty(orderId)) {
      MarketOrderCapsule makerOrderCapsule = orderStore.getUnchecked(orderId);
      while (makerOrderCapsule != null) {
        result.add(makerOrderCapsule);
        makerOrderCapsule = makerOrderCapsule.getNextCapsule(orderStore);
        count++;
        if (count > limit) {
          break;
        }
      }
    }
    return result;
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L339-364)
```java
      MarketOrderIdListCapsule orderIdListCapsule = pairPriceToOrderStore.get(pairPriceKey);

      // match different orders which have the same price
      while (takerCapsule.getSellTokenQuantityRemain() != 0
          && !orderIdListCapsule.isOrderEmpty()) {
        byte[] orderId = orderIdListCapsule.getHead();
        MarketOrderCapsule makerOrderCapsule = orderStore.get(orderId);

        matchSingleOrder(takerCapsule, makerOrderCapsule, ret, takerAccountCapsule);

        // remove order
        if (makerOrderCapsule.getSellTokenQuantityRemain() == 0) {
          // remove from market order list
          orderIdListCapsule.removeOrder(makerOrderCapsule, orderStore,
              pairPriceKey, pairPriceToOrderStore);
        }

        matchOrderCount++;
        if (matchOrderCount > MAX_MATCH_NUM) {
          throw new ContractValidateException("Too many matches. MAX_MATCH_NUM = " + MAX_MATCH_NUM);
        }
      }

      // the orders of makerPrice have been all consumed
      if (orderIdListCapsule.isOrderEmpty()) {
        pairPriceToOrderStore.delete(pairPriceKey);
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketCancelOrderActuator.java (L111-126)
```java
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

      if (orderIdListCapsule.isOrderEmpty()) {
        // if orderList is empty, delete
        pairPriceToOrderStore.delete(pairPriceKey);

```

**File:** framework/src/main/java/org/tron/core/Wallet.java (L2905-2949)
```java
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
```
