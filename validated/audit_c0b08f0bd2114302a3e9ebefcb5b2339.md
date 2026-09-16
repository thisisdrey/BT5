Based on the report's underlying bug class — an on-chain accounting reset (`balance = 0` / order fully consumed) that is driven by an amount value which is computed *independently* of the amount actually being credited back to the counterparty, causing a permanent, unrecoverable shortfall — the closest reachable analog in java-tron is in the **TRC10 market order-matching engine**, `MarketSellAssetActuator.matchSingleOrder`.

### Title
Dust/value loss from independently-computed maker fill amount while maker order is unconditionally zeroed - (File: actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java)

### Summary
In the taker-equals-maker branch of `matchSingleOrder`, the amount credited to the maker (`makerBuyTokenQuantityReceive`) is computed via a separate floor-division ratio instead of being derived from the taker's payment that is actually deducted, while the maker's remaining sell quantity is unconditionally forced to `0` and the maker order is marked `INACTIVE` regardless of whether the credited amount reflects the full value of what was consumed.

### Finding Description
In `matchSingleOrder`, when `takerBuyTokenQuantityRemain == makerOrderCapsule.getSellTokenQuantityRemain()` (the "taker == maker" branch): [1](#0-0) 

`makerBuyTokenQuantityReceive` is computed by `MarketUtils.multiplyAndDivide(makerSellRemainQuantity, makerBuyQuantity, makerSellQuantity, ...)`, which is a *separate*, floor-dividing integer calculation: [2](#0-1) 

That computed value is what actually gets deducted from the taker's remaining sell quantity (`takerSellTokenLeft = takerOrderCapsule.getSellTokenQuantityRemain() - makerBuyTokenQuantityReceive`), and it is what is credited to the maker via `addTrxOrToken(makerOrderCapsule, makerBuyTokenQuantityReceive)`. Meanwhile `makerOrderCapsule.setSellTokenQuantityRemain(0)` unconditionally zeroes the maker's remaining sell balance and marks the maker order `INACTIVE`, removing it from the account's active order list via `MarketUtils.updateOrderState`, with no path to return any shortfall to the maker: [3](#0-2) 

This mirrors the `LiquidationRow` pattern exactly: an amount (`sellAmount`/here `makerBuyTokenQuantityReceive`) is calculated off of a formula that can diverge (due to integer floor-division/rounding) from the value that should correspond to fully consuming the maker's order, yet the underlying balance/order-remaining accounting is wiped to zero unconditionally, permanently discarding any shortfall rather than returning it or reconciling it.

### Impact Explanation
Every match that lands in the "taker == maker" branch can under-credit the maker relative to the true value of the asset it gave up, because `multiplyAndDivide` truncates. Because `setSellTokenQuantityRemain(0)` closes the order unconditionally afterward, the shortfall is not tracked anywhere and cannot be recovered by the maker — it is a systemic, per-trade value leak on the TRC10 asset exchange (`MarketSellAssetContract`/`MarketOrder`), reachable by any account placing sell orders through the public actuator path. Over high trading volume this accumulates into a material, permanent loss of maker funds, analogous to the audited Tokemak issue.

### Likelihood Explanation
This is triggered by ordinary, unprivileged use of the TRC10 market (`MarketSellAssetContract`), requiring no special permissions — any two accounts placing matching sell orders whose relative price ratio doesn't divide evenly can hit this code path. This makes the likelihood high, since it occurs under normal trading activity rather than requiring an adversarial setup.

### Recommendation
In the "taker == maker" branch, derive `makerBuyTokenQuantityReceive` and `takerSellTokenLeft` from a single conservation-preserving computation (e.g., compute the taker's leftover directly from `takerSellRemainQuantity - takerBuyTokenQuantityRemain`'s corresponding TRX value, ensuring the maker is fully credited for the entire `makerSellRemainQuantity` it gives up), or track/reconcile rounding dust so that closing an order to zero remaining balance never discards value that was not actually credited to the counterparty.

### Proof of Concept
Construct a maker order with `sellTokenQuantity` / `buyTokenQuantity` such that the price ratio does not divide evenly (e.g., `makerSellQuantity=3`, `makerBuyQuantity=2`), and a taker order whose `takerBuyTokenQuantityRemain` computed via `multiplyAndDivide` exactly equals `makerSellRemainQuantity`. Execute `MarketSellAssetActuator` for the taker; observe that `makerBuyTokenQuantityReceive = multiplyAndDivide(makerSellRemainQuantity, makerBuyQuantity, makerSellQuantity)` floors below the ideal proportional value while `makerOrderCapsule.setSellTokenQuantityRemain(0)` closes out the maker order — the difference is silently lost, as illustrated by the repo's own "(Accuracy problem)" test naming convention around this matching logic: [4](#0-3)

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L418-439)
```java
    if (takerBuyTokenQuantityRemain == makerOrderCapsule.getSellTokenQuantityRemain()) {
      // taker == maker

      // makerSellTokenQuantityRemain_A/makerBuyTokenQuantityCurrent_TRX =
      //   makerSellTokenQuantity_A/makerBuyTokenQuantity_TRX
      // => makerBuyTokenQuantityCurrent_TRX = makerSellTokenQuantityRemain_A *
      //   makerBuyTokenQuantity_TRX / makerSellTokenQuantity_A

      makerBuyTokenQuantityReceive = MarketUtils
          .multiplyAndDivide(makerSellRemainQuantity, makerBuyQuantity, makerSellQuantity,
              this.disableJavaLangMath());
      takerBuyTokenQuantityReceive = makerOrderCapsule.getSellTokenQuantityRemain();

      long takerSellTokenLeft =
          takerOrderCapsule.getSellTokenQuantityRemain() - makerBuyTokenQuantityReceive;
      takerOrderCapsule.setSellTokenQuantityRemain(takerSellTokenLeft);
      makerOrderCapsule.setSellTokenQuantityRemain(0);

      if (takerSellTokenLeft == 0) {
        MarketUtils.updateOrderState(takerOrderCapsule, State.INACTIVE, marketAccountStore);
      }
      MarketUtils.updateOrderState(makerOrderCapsule, State.INACTIVE, marketAccountStore);
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

**File:** chainbase/src/main/java/org/tron/core/capsule/utils/MarketUtils.java (L264-277)
```java
  public static long multiplyAndDivide(long a, long b, long c, boolean disableMath) {
    try {
      long tmp = multiplyExact(a, b, disableMath);
      return floorDiv(tmp, c, disableMath);
    } catch (ArithmeticException ex) {
      // do nothing here, because we will use BigInteger to compute again
    }

    BigInteger aBig = BigInteger.valueOf(a);
    BigInteger bBig = BigInteger.valueOf(b);
    BigInteger cBig = BigInteger.valueOf(c);

    return aBig.multiply(bBig).divide(cBig).longValue();
  }
```

**File:** framework/src/test/java/org/tron/core/actuator/MarketSellAssetActuatorTest.java (L603-623)
```java
  // execute: combination
  // Trading object：
  //    abc to def
  //    abc to trx
  //    trx to abc
  // Scenes：
  //    no buy orders before,add first sell order
  //    no buy orders before，add multiple sell orders, need to maintain the correct sequence
  //    no buy orders before，add multiple sell orders, need to maintain the correct sequence,
  //      same price
  //    has buy orders before，add first sell order，not match
  //    has buy orders and sell orders before，add sell order，not match,
  //      need to maintain the correct sequence

  //    all match with 2 existing same price buy orders and complete all 3 orders
  //    part match with 2 existing buy orders and complete the makers,
  //        left enough
  //        left not enough and return left（Accuracy problem）
  //    part match with 2 existing buy orders and complete the taker,
  //        left enough
  //        left not enough and return left（Accuracy problem）（not exist)
```
