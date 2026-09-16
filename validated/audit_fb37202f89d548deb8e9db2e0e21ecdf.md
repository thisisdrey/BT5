### Title
Zero-payment token extraction in market order matching due to missing zero-check on maker's exact-match receive amount - ([File: actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java])

### Summary
`MarketSellAssetActuator.matchSingleOrder()` computes the taker's and maker's received quantities from integer-division ratios. In the "taker == maker" exact-match branch, the maker's receive amount (`makerBuyTokenQuantityReceive`) is computed via `MarketUtils.multiplyAndDivide()` but, unlike the sibling "taker > maker" branch, is never checked for a zero result before the trade is executed. When the ratio rounds down to zero, the taker is credited real tokens while paying nothing for that fill, and the maker's order is fully consumed with zero compensation — the same "buy with 0 payment" root cause described in the DODO report, driven here by integer-division truncation on TRC10 order-book ratios instead of decimal mismatch.

### Finding Description
In `matchSingleOrder()`: [1](#0-0) 

The `taker == maker` branch computes:
- `makerBuyTokenQuantityReceive = MarketUtils.multiplyAndDivide(makerSellRemainQuantity, makerBuyQuantity, makerSellQuantity, ...)` — the amount the **maker** receives from the taker
- `takerBuyTokenQuantityReceive = makerOrderCapsule.getSellTokenQuantityRemain()` — the amount the **taker** receives

`multiplyAndDivide` performs floor division: [2](#0-1) 

If `makerSellRemainQuantity * makerBuyQuantity < makerSellQuantity`, this floors to `0`. Unlike the parallel "taker > maker" branch, which explicitly guards against this: [3](#0-2) 

the "taker == maker" branch has **no such guard**. It proceeds directly to: [4](#0-3) 

and then credits both parties unconditionally: [5](#0-4) 

`addTrxOrToken(takerOrderCapsule, takerBuyTokenQuantityReceive, ...)` credits the taker with a nonzero amount, while `addTrxOrToken(makerOrderCapsule, makerBuyTokenQuantityReceive)` credits the maker with `0`. Meanwhile `takerOrderCapsule.setSellTokenQuantityRemain(takerSellTokenLeft)` where `takerSellTokenLeft = takerOrderCapsule.getSellTokenQuantityRemain() - makerBuyTokenQuantityReceive` leaves the taker's remaining sell balance **unchanged** (since `makerBuyTokenQuantityReceive == 0`). The maker's order is simultaneously fully drained (`makerOrderCapsule.setSellTokenQuantityRemain(0)`) and marked `INACTIVE`.

Net effect: the taker receives real tokens from the maker's order and pays nothing (their sell-side balance is untouched), while the maker's entire remaining sell quantity is consumed with zero compensation.

### Impact Explanation
This is a direct, unauthorized theft/loss-of-funds path reachable by any account placing a `MarketSellAssetContract` transaction (`MarketSellAssetActuator`) against an existing resting order on the TRC10 order book. It allows an attacker to extract tokens from a maker's order for free when the exact-match branch triggers with a small enough remaining quantity relative to the maker's sell/buy ratio, causing permanent loss of the maker's assets without any offsetting payment credited to them.

### Likelihood Explanation
Triggering requires the taker's computed buy amount to exactly equal the maker's remaining sell quantity (the natural "exact fill" case, which is common in order-book matching, not a rare edge case), combined with a maker order whose remaining-quantity-to-sellQuantity ratio is small enough that `remain * buyQuantity / sellQuantity` floors to zero (e.g., a partially-filled maker order with `sellTokenQuantity >> buyTokenQuantity`, similar to the scenario the code's own comment near line 469–474 acknowledges can occur for the sibling branch). An attacker can deliberately engineer this by first partially filling a target maker order down to a small remainder, then submitting a precisely sized taker order to hit the unguarded exact-match branch.

### Recommendation
Add the same zero-quantity guard used in the "taker > maker" branch to the "taker == maker" branch: if `makerBuyTokenQuantityReceive == 0`, return the maker's remaining sell tokens (`MarketUtils.returnSellTokenRemain`) and mark the order `INACTIVE` without crediting the taker, instead of executing the trade with a zero-value leg.

### Proof of Concept
1. Maker creates an order selling token A for token B with `sellTokenQuantity = 200`, `buyTokenQuantity = 1` (ratio 200:1).
2. Through prior partial fills, the maker order's `sellTokenQuantityRemain` is reduced to `1`.
3. A taker submits a `MarketSellAssetContract` selling token B for token A such that `takerBuyTokenQuantityRemain` (computed via `MarketUtils.multiplyAndDivide`) equals exactly `1`, matching `makerOrderCapsule.getSellTokenQuantityRemain()`.
4. In `matchSingleOrder`, the `taker == maker` branch computes `makerBuyTokenQuantityReceive = multiplyAndDivide(1, 1, 200) = 0`.
5. The maker's order is fully consumed (`sellTokenQuantityRemain = 0`, state `INACTIVE`) and receives `0` tokens B.
6. The taker's `sellTokenQuantityRemain` is left unchanged (`- 0`), yet `addTrxOrToken` credits the taker with `takerBuyTokenQuantityReceive = 1` unit of token A — obtained for free.

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

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L461-477)
```java
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

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L485-498)
```java
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
