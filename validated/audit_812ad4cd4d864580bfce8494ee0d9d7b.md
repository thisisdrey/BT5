### Title
Systematic floor-rounding in `MarketSellAssetActuator.matchSingleOrder()` lets the taker (order placer) extract value from maker orders - (File: `actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java`)

### Summary
`MarketSellAssetActuator` implements TRON's on-chain order-book market (`MarketSellAssetContract`, reachable by any account via a signed transaction). When a taker order matches resting maker orders, the amount the taker must pay the maker is computed with `MarketUtils.multiplyAndDivide()`, which always floors (`floorDiv`) the result. Exactly like the reported `_convertValueInUsdToValueInNumeraire()` bug — where a rounded-down denominator makes a division result larger than the true value and inflates value in favor of the caller — the repeated floor-division here always understates the payment owed to the resting maker order and lets the taker (the transaction sender) acquire the maker's full remaining inventory for less than the maker's specified price.

### Finding Description
In `matchSingleOrder()` [1](#0-0) , the taker's obtainable quantity is computed with `MarketUtils.multiplyAndDivide`, which internally uses `floorDiv` [2](#0-1) .

When the taker fully consumes the maker's remaining inventory (the "taker == maker" and "taker > maker" branches), `makerBuyTokenQuantityReceive` — the amount of the taker's sell-token that must be paid to the maker for that inventory — is computed the same way, again always rounding down: [3](#0-2) [4](#0-3) 

In both branches, the taker receives the maker's *entire* remaining sell-token balance (`makerOrderCapsule.getSellTokenQuantityRemain()` for the equal case, or the full remainder in the "taker > maker" case) while paying only the floored `makerBuyTokenQuantityReceive` amount, and any left-over of the taker's originally-committed sell tokens (`takerSellTokenLeft` / the residual after `subtractExact`) is kept by the taker rather than being fully consumed to match the maker's own price ratio. Because the division always floors, `makerBuyTokenQuantityReceive` is always less than or equal to the mathematically exact amount required by the maker's price ratio (`makerSellQuantity : makerBuyQuantity`), so the maker's order is systematically settled at a value below what it originally set as an on-chain price.

This mirrors the reported bug class exactly: a division that always rounds in the same direction (down) is used to compute a value transferred between two parties, and that direction consistently favors the party who initiates the operation (the taker/order placer) at the expense of the counterparty (the maker), rather than being neutral or rounding in the protocol's/counterparty's favor.

### Impact Explanation
Any account can place `MarketSellAssetContract` transactions matching against existing resting orders. Because the shortfall always favors the taker, an attacker can:
- Repeatedly place small taker orders against a target maker order to drain value below the maker's specified price, extracting value that legitimately belongs to the maker with every match.
- This is compounded across every trade in the order book because the rounding direction is deterministic and never favors the maker, unlike price impact which is symmetric.

This results in unauthorized value extraction from maker accounts' effective asset value over many trades, a fund-safety impact in an unprivileged, permissionless code path (`MarketSellAssetActuator`, reachable by any signed transaction).

### Likelihood Explanation
High for at least partial exploitation: any user can create maker orders and any user can act as taker; the floor-rounding behavior is deterministic and requires no special conditions, only enough trades/matches to accumulate a meaningful discrepancy. `MAX_MATCH_NUM` limits the number of orders matched per transaction but does not prevent repeated transactions.

### Recommendation
When computing `makerBuyTokenQuantityReceive` (the amount owed to a maker being fully consumed), round in favor of the maker (round up) rather than always flooring, or otherwise ensure the taker always pays at least the maker's fair price. Review `MarketUtils.multiplyAndDivide()` usage throughout `MarketSellAssetActuator.matchSingleOrder()` to make the rounding direction consistently protect the resting order's original price rather than always benefiting the taker.

### Proof of Concept
1. Maker places a sell order at price ratio `makerSellQuantity : makerBuyQuantity` (e.g. sell 3 A for 7 B — a ratio chosen so `multiplyAndDivide` truncates a fractional unit).
2. Taker places a matching buy order that consumes the maker's order in a single match ("taker == maker" or "taker > maker" branch in `matchSingleOrder()`).
3. `makerBuyTokenQuantityReceive = floorDiv(makerSellRemainQuantity * makerBuyQuantity, makerSellQuantity)` truncates a fraction of a unit that the maker was owed by the specified price ratio.
4. The taker receives the maker's full sell-token remainder while paying the truncated (lower) amount — the taker's actual price paid is strictly better than the maker's posted price, and the difference is not returned to the maker; it is retained by the taker as unspent balance of their own order.
5. Repeating this with many small trades allows systematic, cumulative extraction of value from maker orders network-wide.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L402-404)
```java
    long takerBuyTokenQuantityRemain = MarketUtils
        .multiplyAndDivide(takerSellRemainQuantity, makerSellQuantity, makerBuyQuantity,
            this.disableJavaLangMath());
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L418-452)
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
    } else if (takerBuyTokenQuantityRemain < makerOrderCapsule.getSellTokenQuantityRemain()) {
      // taker < maker
      // if the quantity of taker want to buy is smaller than the remain of maker want to sell,
      // consume the order of the taker

      takerBuyTokenQuantityReceive = takerBuyTokenQuantityRemain;
      makerBuyTokenQuantityReceive = takerOrderCapsule.getSellTokenQuantityRemain();

      takerOrderCapsule.setSellTokenQuantityRemain(0);
      MarketUtils.updateOrderState(takerOrderCapsule, State.INACTIVE, marketAccountStore);

      makerOrderCapsule.setSellTokenQuantityRemain(subtractExact(
          makerOrderCapsule.getSellTokenQuantityRemain(), takerBuyTokenQuantityRemain));
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L453-482)
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
      } else {
        makerOrderCapsule.setSellTokenQuantityRemain(0);
        takerOrderCapsule.setSellTokenQuantityRemain(subtractExact(
            takerOrderCapsule.getSellTokenQuantityRemain(), makerBuyTokenQuantityReceive));
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
