### Title
Unauthorized invalidation of maker orders in `MarketSellAssetActuator` via dust-amount taker fills - (File: `actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java`)

### Summary
Similar to the `OrderRFQMixin` issue where a taker can invalidate a maker's signed order with a dust amount, `java-tron`'s on-chain order book (`MarketSellAssetActuator`) allows any account to force a maker's active, resting order into `State.INACTIVE` by matching it with a specifically crafted, cheap taker order — without the maker receiving any of the proceeds they are owed for that fill.

### Finding Description
In `matchSingleOrder`, when the taker's desired-buy amount is larger than the maker's remaining sell amount ("taker > maker" branch), the code marks the maker's order `INACTIVE` *before* checking whether the computed proceeds for the maker are non-zero: [1](#0-0) 

```
} else {
  // taker > maker
  takerBuyTokenQuantityReceive = makerOrderCapsule.getSellTokenQuantityRemain();
  makerBuyTokenQuantityReceive = MarketUtils.multiplyAndDivide(...);
  MarketUtils.updateOrderState(makerOrderCapsule, State.INACTIVE, marketAccountStore);
  if (makerBuyTokenQuantityReceive == 0) {
    // quantity too small, return the remain of sellToken to maker
    makerOrderCapsule.setSellTokenQuantityReturn();
    returnSellTokenRemain(makerOrderCapsule);
    return;
  } ...
```

`updateOrderState` removes the order from the maker's active order list and persists `State.INACTIVE`, unconditionally: [2](#0-1) 

Because the integer-division helper `multiplyAndDivide` rounds down, `makerBuyTokenQuantityReceive` can be exactly `0` whenever `makerSellRemainQuantity * makerBuyQuantity < makerSellQuantity` — a condition that is easy for an attacker to engineer by choosing a sell amount that fully consumes the maker's remaining sell quantity while yielding a proportional buy-side return that rounds to zero (this exact rounding edge case is acknowledged in-repo, see the "Accuracy problem" test comments in `MarketSellAssetActuatorTest`). Since the entire order book (prices, remaining quantities) is publicly queryable, an attacker can pick target orders and construct a `MarketSellAssetContract` whose `sellTokenQuantity`/`buyTokenQuantity` ratio drives this exact zero-proceeds outcome.

The result: the maker's order is force-cancelled (`INACTIVE`) and removed from `MarketAccountOrderCapsule`/`MarketPairPriceToOrderStore`, exactly like the `OrderRFQMixin` invalidation bug, while the attacker pays only `dynamicStore.getMarketSellFee()` (a small fixed fee), which is far cheaper than the maker's own `getMarketCancelFee()` used by `MarketCancelOrderActuator`: [3](#0-2) 

### Impact Explanation
Makers lose their queue position and standing liquidity in the order book without their consent and without compensation for the "fill" (since proceeds rounded to zero), making the on-chain order book unreliable for market makers — mirroring the exact impact described in the report (protocol becomes impractical for makers because attackers can invalidate orders cheaply). While the maker's remaining sell-side tokens are returned (no direct fund loss), this is a griefing/DoS vector against exchange functionality that a single unprivileged account can trigger repeatedly and cheaply against arbitrary maker orders, since order book contents (`MarketPairPriceToOrderStore`, `MarketPairToPriceStore`) are queryable via the wallet API.

### Likelihood Explanation
High likelihood: the attack requires only a single `MarketSellAssetContract` transaction with attacker-chosen quantities against a publicly visible target order; no special privileges, timing races, or partial-fill history are required, and the fee cost is a small fixed `MarketSellFee`.

### Proof of Concept
1. Attacker queries the order book (`getmarketorderlistbypair`/`getmarketpricebypair`) to find an active maker order with `sellTokenQuantity`/`buyTokenQuantity` ratio `S/B`.
2. Attacker submits a `MarketSellAssetContract` (via `MarketSellAssetActuator`) with a sell quantity chosen so that, in `matchSingleOrder`, the "taker > maker" branch is hit and `makerSellRemainQuantity * makerBuyQuantity / makerSellQuantity == 0` (round-down to zero).
3. `matchSingleOrder` calls `MarketUtils.updateOrderState(makerOrderCapsule, State.INACTIVE, marketAccountStore)` unconditionally, then detects `makerBuyTokenQuantityReceive == 0`, returns the maker's remaining sell tokens, and exits — the maker's order is now cancelled/removed from the order book, having received zero proceeds for the "fill," at the cost of only the attacker's `MarketSellFee`. [4](#0-3) 

### Recommendation
Move `MarketUtils.updateOrderState(makerOrderCapsule, State.INACTIVE, ...)` in `matchSingleOrder`'s "taker > maker" branch to *after* the `makerBuyTokenQuantityReceive == 0` check, so a zero-proceeds match does not deactivate/remove the maker's order — instead, either skip the match entirely (treat as no fill) or require a minimum non-zero proceeds threshold before consuming/cancelling a maker order, analogous to the taker-side dust-protection already present at lines 406-413 of `MarketSellAssetActuator.java`.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L453-477)
```java
    } else {
      // taker > maker
      takerBuyTokenQuantityReceive = makerOrderCapsule.getSellTokenQuantityRemain();

      // if the quantity of taker want to buy is bigger than the remain of maker want to sell,
      // consume the order of maker
      // makerSellTokenQuantityRemain_A/makerBuyTokenQuantityCurrent_TRX =
      //   makerSellTokenQuantity_A/makerBuyTokenQuantity_TRX
      makerBuyTokenQuantityReceive = MarketUtils
          .multiplyAndDivide(makerSellRemainQuantity, makerBuyQuantity, makerSellQuantity,
              this.disableJavaLangMath());

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

**File:** chainbase/src/main/java/org/tron/core/capsule/utils/MarketUtils.java (L251-262)
```java
  public static void updateOrderState(MarketOrderCapsule orderCapsule,
      State state, MarketAccountStore marketAccountStore) throws ItemNotFoundException {
    orderCapsule.setState(state);

    // remove from account order list
    if (state == State.INACTIVE || state == State.CANCELED) {
      MarketAccountOrderCapsule accountOrderCapsule = marketAccountStore
          .get(orderCapsule.getOwnerAddress().toByteArray());
      accountOrderCapsule.removeOrder(orderCapsule.getID());
      marketAccountStore.put(accountOrderCapsule.createDbKey(), accountOrderCapsule);
    }
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketCancelOrderActuator.java (L84-102)
```java
    long fee = calcFee();

    try {
      final MarketCancelOrderContract contract = this.any
          .unpack(MarketCancelOrderContract.class);

      AccountCapsule accountCapsule = accountStore
          .get(contract.getOwnerAddress().toByteArray());

      byte[] orderId = contract.getOrderId().toByteArray();
      MarketOrderCapsule orderCapsule = orderStore.get(orderId);

      // fee
      accountCapsule.setBalance(accountCapsule.getBalance() - fee);
      if (dynamicStore.supportBlackHoleOptimization()) {
        dynamicStore.burnTrx(fee);
      } else {
        adjustBalance(accountStore, accountStore.getBlackhole(), fee);
      }
```

**File:** framework/src/test/java/org/tron/core/actuator/MarketSellAssetActuatorTest.java (L1714-1719)
```java
  /**
   * match with 2 existing buy orders and complete the maker, taker left not enough and return
   * left（Accuracy problem）
   */
  @Test
  public void partMatchMakerLeftNotEnoughBuyOrders1() throws Exception {
```
