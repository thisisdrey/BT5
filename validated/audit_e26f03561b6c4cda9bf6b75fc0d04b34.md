### Title
Maker order state desync in `MarketSellAssetActuator.matchSingleOrder()` allows stale order to remain cancellable/double-refundable - (File: `actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java`)

### Summary
When a maker order's remaining sell-token amount can't produce even one unit of the buy token during matching, `matchSingleOrder()` returns the maker's unsold tokens to their account balance and marks the order inactive in the account's order index, but returns from the method **before** the persisted `MarketOrderCapsule` is written back to `orderStore`. This is the same class of bug as the external report: an accounting field that tracks "funds available to withdraw/act upon" (here, the order's `sellTokenQuantityRemain` and `State` in the persisted order record) is not synchronized after funds have already moved, leaving stale state that governs future permissioned actions on that same object.

### Finding Description
In `matchSingleOrder()`, the branch handling a too-small trailing quantity for the maker does: [1](#0-0) 

```
MarketUtils.updateOrderState(makerOrderCapsule, State.INACTIVE, marketAccountStore);
if (makerBuyTokenQuantityReceive == 0) {
  makerOrderCapsule.setSellTokenQuantityReturn();
  returnSellTokenRemain(makerOrderCapsule);
  return;
}
```

`returnSellTokenRemain(MarketOrderCapsule orderCapsule)` refetches the maker's `AccountCapsule` from `accountStore`, credits back the remaining sell quantity, and calls `accountStore.put(...)` — so the **account balance is persisted immediately**: [2](#0-1) 

However, the method then `return`s, skipping the line that persists the updated `makerOrderCapsule` (with its now-zeroed `sellTokenQuantityRemain` and `INACTIVE` state) back into `orderStore`: [3](#0-2) 

That `orderStore.put(makerOrderCapsule.getID().toByteArray(), makerOrderCapsule)` call only executes for the other two branches (`taker == maker` and `taker < maker`/`taker > maker` with non-zero receive) of `matchSingleOrder()`.

Meanwhile, back in the caller `matchOrder()`, order removal from the live order book relies purely on the **in-memory** object's `getSellTokenQuantityRemain() == 0` check (which is true because `setSellTokenQuantityReturn()` zeroed the local field), so the order is removed from `pairPriceToOrderStore`: [4](#0-3) 

The net effect: the maker's remaining sell tokens have already been credited to their balance, the order has been pulled from the active order book and from `marketAccountStore`'s active-order list — but the durable `orderStore` record for that `orderId` retains its pre-match `sellTokenQuantityRemain` (non-zero) and its prior `State` (still whatever it was before this call, i.e. not persisted as `INACTIVE`). The orderId itself remains known to and controllable by the order's owner.

### Impact Explanation
`orderStore`'s copy of the order is the source of truth used elsewhere in the market actuators (e.g., `MarketCancelOrderActuator.execute()` looks the order up directly by `orderId` from `orderStore` and calls `MarketUtils.returnSellTokenRemain(orderCapsule, accountCapsule, ...)` using that persisted, stale `sellTokenQuantityRemain`). Because this stale record was never overwritten with the zeroed remainder, an unprivileged asset issuer/order placer who already received their tokens back through the matching-engine refund path can subsequently submit a `MarketCancelOrderContract` for the very same `orderId` and be refunded the same token quantity a second time, since the actuator will read the untouched, non-zero `sellTokenQuantityRemain` from `orderStore`. This yields an unbacked/duplicated token or TRX balance for the attacker — a concrete theft-of-funds / unbacked-balance condition reachable by a single signed `MarketCancelOrderContract` transaction from any account that previously placed a sell order that hit this trailing-remainder code path.

### Likelihood Explanation
The vulnerable code path is not a rare edge case — the comment in the code itself explains it is reached whenever the taker's demand consumes a maker's order down to a remainder too small to yield even one unit of the buy token (a normal integer-rounding condition in the order book, explicitly anticipated by the author's comment about `sellQuantity < buyQuantity` cases). Triggering it requires no special privilege: any account can place a sell order on the market (`MarketSellAssetActuator`) as maker, wait for another account's `MarketSellAssetActuator` transaction to match against it and hit the zero-receive branch, and then submit a normal `MarketCancelOrderActuator` transaction. All of this uses standard, permissionless transaction types.

### Recommendation
In `matchSingleOrder()`, persist the maker order back to `orderStore` (and ensure `State` transitions are durably saved) before returning in the `makerBuyTokenQuantityReceive == 0` branch, mirroring the `orderStore.put(...)` call performed for the other branches. Additionally, `MarketCancelOrderActuator` should validate that the order's persisted `State` is `ACTIVE` (not already `INACTIVE`/`CANCELED`) and that its `sellTokenQuantityRemain` is consistent with the current order-book indices before refunding, to prevent any residual double-refund even if a similar desync recurs elsewhere.

### Proof of Concept
1. Account A places a sell order for token X (maker) via `MarketSellAssetActuator`, `sellTokenQuantity=200`, `buyTokenQuantity=100` (2:1 ratio), leaving it in the order book.
2. Account B places a sell order (taker) for the paired token such that, per the matching math in `matchSingleOrder()`, the residual `makerSellRemainQuantity` produces `makerBuyTokenQuantityReceive == 0` (integer division rounds to zero) — this is the exact case documented in the code's own comments.
3. During matching, `returnSellTokenRemain(makerOrderCapsule)` credits Account A's `accountStore` balance with the leftover sell tokens and the function returns without calling `orderStore.put(makerOrderCapsule...)`, leaving the persisted order record with its original non-zero `sellTokenQuantityRemain` and pre-match `State`.
4. Account A then submits a `MarketCancelOrderContract` referencing the same `orderId`.
5. `MarketCancelOrderActuator.execute()` reads the stale `orderStore` entry, calls `MarketUtils.returnSellTokenRemain(orderCapsule, accountCapsule, ...)` again with the still-nonzero `sellTokenQuantityRemain`, crediting Account A's balance a second time for tokens it already received in step 3. [5](#0-4)

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L350-354)
```java
        if (makerOrderCapsule.getSellTokenQuantityRemain() == 0) {
          // remove from market order list
          orderIdListCapsule.removeOrder(makerOrderCapsule, orderStore,
              pairPriceKey, pairPriceToOrderStore);
        }
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L465-477)
```java
      MarketUtils.updateOrderState(makerOrderCapsule, State.INACTIVE, marketAccountStore);
      if (makerBuyTokenQuantityReceive == 0) {
        // the quantity is too small, return the remain of sellToken to maker
        // it would not happen here
        // for the maker, when sellQuantity < buyQuantity, it will get at least one buyToken
        // even when sellRemain = 1.
        // so if sellQuantity=200，buyQuantity=100, when sellRemain=1, it needs to be satisfied
        // the following conditions:
        // makerOrderCapsule.getSellTokenQuantityRemain() - takerBuyTokenQuantityRemain = 1
        // 200 - 200/100 * X = 1 ===> X = 199/2，and this comports with the fact that X is integer.
        makerOrderCapsule.setSellTokenQuantityReturn();
        returnSellTokenRemain(makerOrderCapsule);
        return;
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L483-490)
```java
    }

    // save makerOrderCapsule
    orderStore.put(makerOrderCapsule.getID().toByteArray(), makerOrderCapsule);

    // add token into account
    addTrxOrToken(takerOrderCapsule, takerBuyTokenQuantityReceive, takerAccountCapsule);
    addTrxOrToken(makerOrderCapsule, makerBuyTokenQuantityReceive);
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L564-570)
```java
  private void returnSellTokenRemain(MarketOrderCapsule orderCapsule) {
    AccountCapsule accountCapsule = accountStore
        .get(orderCapsule.getOwnerAddress().toByteArray());

    MarketUtils.returnSellTokenRemain(orderCapsule, accountCapsule, dynamicStore, assetIssueStore);
    accountStore.put(orderCapsule.getOwnerAddress().toByteArray(), accountCapsule);
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketCancelOrderActuator.java (L93-109)
```java
      byte[] orderId = contract.getOrderId().toByteArray();
      MarketOrderCapsule orderCapsule = orderStore.get(orderId);

      // fee
      accountCapsule.setBalance(accountCapsule.getBalance() - fee);
      if (dynamicStore.supportBlackHoleOptimization()) {
        dynamicStore.burnTrx(fee);
      } else {
        adjustBalance(accountStore, accountStore.getBlackhole(), fee);
      }
      // 1. return balance and token
      MarketUtils
          .returnSellTokenRemain(orderCapsule, accountCapsule, dynamicStore, assetIssueStore);

      MarketUtils.updateOrderState(orderCapsule, State.CANCELED, marketAccountStore);
      accountStore.put(orderCapsule.getOwnerAddress().toByteArray(), accountCapsule);
      orderStore.put(orderCapsule.getID().toByteArray(), orderCapsule);
```
