## Analysis: Rounding-down division allows zero-payment order fills in `MarketSellAssetActuator`

The reported bug class — a "shares/amount owed" quantity computed via floor division that can round down to `0` while a nonzero counter-value is still transferred — has a direct analog in the TRC10 exchange market matching engine reachable by any account submitting a `MarketSellAssetContract` transaction.

### Root cause

`MarketSellAssetActuator.matchSingleOrder` computes cross-rates with `MarketUtils.multiplyAndDivide`, which is a floor division: [1](#0-0) 

In the `taker == maker` branch, `makerBuyTokenQuantityReceive` (what the maker gets paid) is derived from that same floor division, but — unlike the `taker > maker` branch a few lines below, which explicitly checks `if (makerBuyTokenQuantityReceive == 0)` and refunds the maker instead of completing the trade — the equal-quantity branch has **no such guard**: [2](#0-1) 

Compare with the guarded branch: [3](#0-2) 

If `makerSellRemainQuantity * makerBuyQuantity < makerSellQuantity` (i.e., the maker's remaining sell balance has been driven down by prior partial fills to the point where its proportional buy-token entitlement rounds to zero), then when a taker's order exactly equals `makerOrderCapsule.getSellTokenQuantityRemain()`, the code takes the `taker == maker` branch, sets `makerBuyTokenQuantityReceive = 0`, marks the maker order `INACTIVE`, and still transfers the full `takerBuyTokenQuantityReceive = makerOrderCapsule.getSellTokenQuantityRemain()` to the taker via `addTrxOrToken`: [4](#0-3) 

This is the same class of bug as the `LiquidityPool.borrow` issue: a proportional-share calculation that floors to `0` is used without a zero-check, letting one counterparty walk away with tokens while crediting the other side nothing.

### Reachability and impact

- Reachable by any unprivileged account: creating maker/taker orders via `MarketSellAssetContract` requires no special permission — it's an ordinary signed transaction handled by `MarketSellAssetActuator`.
- A maker order that sells a large quantity for a very small buy-token amount (`makerSellQuantity >> makerBuyQuantity`) and has been partially filled down to a small remainder is exploitable: whichever taker submits an order whose computed `takerBuyTokenQuantityRemain` exactly equals that small remainder captures the remaining maker-escrowed sell tokens while the maker receives `0` buy tokens for that final fill — this is a direct theft of the maker's escrowed asset balance from `MarketAccountStore`.
- The `taker > maker` branch's inline comment ("it would not happen here … it needs to be satisfied … 200 - 200/100 * X = 1") shows the developers were aware of this exact rounding edge case for one code path and defended it, but missed adding the equivalent guard to the `taker == maker` branch, indicating an inconsistent/incomplete fix rather than a deliberate design choice.

### Title
Missing zero-check on floor-divided `makerBuyTokenQuantityReceive` in the taker==maker branch of `MarketSellAssetActuator.matchSingleOrder` allows a taker to receive maker's escrowed TRC10 tokens for zero payment - (File: `actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java`)

### Summary
`MarketSellAssetActuator.matchSingleOrder` uses `MarketUtils.multiplyAndDivide` (a floor division helper) to compute how many buy-tokens a maker order receives when its remaining sell quantity is fully consumed by an exactly-matching taker order. Unlike the parallel `taker > maker` branch, which checks for a zero result and refunds the maker, the `taker == maker` branch performs no such check, letting the maker's payment silently round down to `0` while the order is closed and the taker still receives the full remaining sell-token balance.

### Finding Description
In `matchSingleOrder`, when `takerBuyTokenQuantityRemain == makerOrderCapsule.getSellTokenQuantityRemain()`, `makerBuyTokenQuantityReceive` is computed as `multiplyAndDivide(makerSellRemainQuantity, makerBuyQuantity, makerSellQuantity, ...)`, which performs `floorDiv(a*b, c)`. When the maker's remaining sell balance is small relative to the ratio `makerSellQuantity : makerBuyQuantity` (i.e. `makerSellRemainQuantity * makerBuyQuantity < makerSellQuantity`), this floors to `0`. The code proceeds to mark the maker order `INACTIVE`, zero its remaining sell quantity, and unconditionally call `addTrxOrToken(makerOrderCapsule, 0)` and `addTrxOrToken(takerOrderCapsule, takerBuyTokenQuantityReceive, ...)`, crediting the taker with the maker's full remaining sell-token balance while the maker's account is credited `0`. The sibling `taker > maker` branch explicitly guards against exactly this scenario with `if (makerBuyTokenQuantityReceive == 0) { ... return; }`, confirming the rounding hazard was known but the fix was not applied uniformly.

### Impact Explanation
This allows theft of a maker's escrowed TRC10/TRX order balance: any account can act as taker and, by matching the exact remaining quantity of a partially-filled, disproportionately-priced maker order, obtain the maker's remaining tokens while paying nothing. Repeated exploitation against orders created by other users (or crafted by the attacker across paired maker/taker orders they control to first "grind down" a maker order's remainder) results in direct loss of funds for the order owner and unbacked token issuance/transfer out of the market escrow. This is a Medium/High severity fund-theft bug consistent with the referenced report's classification.

### Likelihood Explanation
Likelihood is High: exploitation only requires ordinary `MarketSellAssetContract` transactions with attacker-chosen quantities — no special privileges, no reliance on other actors' mistakes beyond the existence of a lopsided-ratio order (which an attacker can itself create and partially self-fill using a second account to engineer the exact vulnerable remainder), and no timing or race constraints.

### Recommendation
Add the same zero-result guard used in the `taker > maker` branch to the `taker == maker` branch: if `makerBuyTokenQuantityReceive` computed via `MarketUtils.multiplyAndDivide` is `0`, return the maker's remaining sell tokens via `MarketUtils.returnSellTokenRemain` instead of closing the order and transferring tokens to the taker for free. More broadly, review `MarketUtils.multiplyAndDivide` usages in `MarketSellAssetActuator` to ensure every consumer path validates for a zero result (or switches to round-up semantics on the side representing the payer's amount owed) before crediting the counterparty.

### Proof of Concept
1. Attacker (account A) creates a maker sell order: sells `makerSellQuantity` units of asset X for `makerBuyQuantity` units of asset Y, where `makerSellQuantity >> makerBuyQuantity` (e.g., sell 1,000,000 X for 1 Y).
2. Using a second account (B), the attacker submits a sequence of taker buy orders that progressively consume the maker order (each match landing in the `taker < maker` branch), driving `makerOrderCapsule.getSellTokenQuantityRemain()` down to a small remainder `R` such that `R * makerBuyQuantity < makerSellQuantity` (i.e. `floor(R * makerBuyQuantity / makerSellQuantity) == 0`).
3. Attacker submits a final taker order (from account B or a third account C) whose computed `takerBuyTokenQuantityRemain` exactly equals `R`, hitting the `taker == maker` branch in `matchSingleOrder`.
4. `makerBuyTokenQuantityReceive` evaluates to `0` via the floor division in `MarketUtils.multiplyAndDivide`; the maker order is closed (`INACTIVE`), and `addTrxOrToken` transfers the full remaining `R` units of asset X to the taker while crediting the maker `0` units of asset Y — the last chunk of the maker's escrowed tokens is taken for free. [5](#0-4)

### Citations

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

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L418-499)
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
    }

    // save makerOrderCapsule
    orderStore.put(makerOrderCapsule.getID().toByteArray(), makerOrderCapsule);

    // add token into account
    addTrxOrToken(takerOrderCapsule, takerBuyTokenQuantityReceive, takerAccountCapsule);
    addTrxOrToken(makerOrderCapsule, makerBuyTokenQuantityReceive);

    MarketOrderDetail orderDetail = MarketOrderDetail.newBuilder()
        .setMakerOrderId(makerOrderCapsule.getID())
        .setTakerOrderId(takerOrderCapsule.getID())
        .setFillSellQuantity(makerBuyTokenQuantityReceive)
        .setFillBuyQuantity(takerBuyTokenQuantityReceive)
        .build();
    ret.addOrderDetails(orderDetail);
  }
```
