## Analysis Result

Based on the evidence gathered, I found a concrete analog of the CVE-2026-60023 bug class (a single-item "get by ID" read path failing to apply the sanitization/authorization logic that the corresponding list path applies) reachable from any anonymous API caller.

### Title
Unauthorized disclosure of internal market-order linked-list pointers via `getMarketOrderById` - (File: `framework/src/main/java/org/tron/core/Wallet.java`)

### Summary
`Wallet.getMarketOrderById` returns the raw `MarketOrderCapsule` instance straight from `MarketOrderStore` without clearing the internal `prev`/`next` linked-list pointer fields, while the sibling "list" query paths (`getMarketOrderByAccount`, `getMarketOrderListByPair`) explicitly strip these same fields before returning results to the API caller.

### Finding Description
The on-chain market order book is implemented as a set of doubly linked lists keyed by price, using `prev`/`next` fields inside each `MarketOrderCapsule` to point to the previous/next order's ID at the same price level (`chainbase/src/main/java/org/tron/core/capsule/MarketOrderIdListCapsule.java:77-134` maintains this list and mutates `prev`/`next` on every order add/remove/cancel/match).

The two list-oriented query paths in `Wallet.java` are aware that these `prev`/`next` fields are internal bookkeeping data not meant for external exposure, and explicitly zero them out before serializing the response: [1](#0-0) [2](#0-1) 

However, `getMarketOrderById` — reachable via the HTTP endpoint `/wallet/getmarketorderbyid` (`GetMarketOrderByIdServlet`), its Solidity/PBFT mirrors, and the gRPC `GetMarketOrderById` RPC — fetches the capsule directly from `MarketOrderStore` and returns `.getInstance()` unmodified, without the same stripping performed by the list-based paths: [3](#0-2) 

This mirrors the reported bug class precisely: a "single item" read path omits a filtering/sanitization step that a "list" read path applies, exposing internal state that was not intended to be surfaced to unauthorized/anonymous callers. Any transaction (an order placed via `MarketSellAssetActuator`) or an order ID discovered from any prior public data — this API takes an arbitrary order ID as input and requires no authentication — can be queried directly through the HTTP/gRPC/JSON endpoints.

### Impact Explanation
The leaked `prev`/`next` fields expose internal store linkage between market orders (including orders belonging to other accounts, and orders that have already been matched/canceled but remain in the underlying `MarketOrderStore` in `CANCELED`/`INACTIVE` state per `MarketUtils.updateOrderState`). This reveals the internal linked-list topology of the order book and other users' order IDs that were never intended to be reconstructable from a single order lookup, which is an information-disclosure issue consistent in class with the reported CVE (unauthorized disclosure of content that should have been filtered/hidden).

### Likelihood Explanation
Trivially reachable: `getMarketOrderById` is exposed unauthenticated over HTTP (`GetMarketOrderByIdServlet`), gRPC (`RpcApiService`), and Solidity/PBFT read-only mirrors, and requires only a 32-byte order ID as input — no signature or permission check is performed on this query path.

### Recommendation
In `Wallet.getMarketOrderById`, clear the `prev`/`next` fields on the returned `MarketOrderCapsule` before calling `.getInstance()`, matching the sanitization already performed in `getMarketOrderByAccount` and `getMarketOrderListByPair`.

### Proof of Concept
1. Submit a `MarketSellAssetContract` transaction to create an order (or observe existing order IDs via `getMarketOrderByAccount`, which itself omits `prev`/`next` in the response but confirms an order ID exists).
2. Call `GET /wallet/getmarketorderbyid?value=<orderIdHex>` (or the gRPC `GetMarketOrderById`), for any order ID.
3. Observe that the raw JSON/protobuf response includes non-empty `prev` and `next` byte fields pointing to adjacent order IDs in the price-level linked list — data that `getmarketorderbyaccount`/`getmarketorderlistbypair` explicitly scrub, confirming the divergence between the single-item and list read paths.

**Note on confidence:** I was unable to directly inspect the `MarketOrder` protobuf message definition or the full `MarketOrderCapsule.java` source (tool errors prevented retrieval in the final iteration), so I could not fully confirm the exact semantic sensitivity of the `prev`/`next` byte content beyond what is evidenced by the explicit stripping logic in `getMarketOrderByAccount`/`getMarketOrderListByPair`. This is a lower-severity information-disclosure analog (internal store topology/other users' order IDs) rather than a fund-theft or crash-level bug, and the reviewer should independently verify the sensitivity/exploitability threshold against the "Medium/High/Critical only" scope requirement before treating it as fully validated.

### Citations

**File:** framework/src/main/java/org/tron/core/Wallet.java (L2794-2809)
```java
  public MarketOrder getMarketOrderById(ByteString orderId) {

    if (orderId == null || orderId.isEmpty()) {
      return null;
    }

    MarketOrderStore marketOrderStore = dbManager.getChainBaseManager().getMarketOrderStore();

    try {
      return marketOrderStore.get(orderId.toByteArray()).getInstance();
    } catch (ItemNotFoundException e) {
      logger.warn("orderId = {} not found", orderId);
      throw new IllegalStateException("order not found in store");
    }

  }
```

**File:** framework/src/main/java/org/tron/core/Wallet.java (L2830-2839)
```java
    orderIdList.forEach(
        orderId -> {
          try {
            MarketOrderCapsule orderCapsule = marketOrderStore.get(orderId.toByteArray());
            // set prev and next, hide these messages in the print
            orderCapsule.setPrev(new byte[0]);
            orderCapsule.setNext(new byte[0]);

            marketOrderListBuilder
                .addOrders(orderCapsule.getInstance());
```

**File:** framework/src/main/java/org/tron/core/Wallet.java (L2940-2946)
```java
        orderList.forEach(orderCapsule -> {
          // set prev and next, hide these messages in the print
          orderCapsule.setPrev(new byte[0]);
          orderCapsule.setNext(new byte[0]);

          builder.addOrders(orderCapsule.getInstance());
        });
```
