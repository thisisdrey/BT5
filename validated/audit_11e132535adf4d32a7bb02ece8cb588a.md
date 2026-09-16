### Title
Missing zero-fill guard in the "taker == maker" branch of order matching causes maker to lose their entire remaining sell-token quantity for zero return - ([File: actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java])

### Summary
`MarketSellAssetActuator.matchSingleOrder` computes the amount a maker receives via `MarketUtils.multiplyAndDivide` (an integer `a*b/c` ratio calculation, structurally identical to the reported `convertToShares` pattern: `amount * numerator / denominator`, which rounds down to `0` when the denominator dominates). In two of the three match branches ("taker < maker" implicitly avoids the formula, and "taker > maker") the code explicitly checks `if (makerBuyTokenQuantityReceive == 0)` and refunds the maker's tokens instead of consuming the order. The "taker == maker" branch performs the exact same division but has **no such check**, unconditionally zeroing out the maker's remaining quantity and marking the order `INACTIVE` even when `makerBuyTokenQuantityReceive == 0`.

### Finding Description
In `matchSingleOrder`, when `takerBuyTokenQuantityRemain == makerOrderCapsule.getSellTokenQuantityRemain()` (the "taker == maker" branch): [1](#0-0) 

```
makerBuyTokenQuantityReceive = MarketUtils
    .multiplyAndDivide(makerSellRemainQuantity, makerBuyQuantity, makerSellQuantity, ...);
takerBuyTokenQuantityReceive = makerOrderCapsule.getSellTokenQuantityRemain();

long takerSellTokenLeft = takerOrderCapsule.getSellTokenQuantityRemain() - makerBuyTokenQuantityReceive;
takerOrderCapsule.setSellTokenQuantityRemain(takerSellTokenLeft);
makerOrderCapsule.setSellTokenQuantityRemain(0);
...
MarketUtils.updateOrderState(makerOrderCapsule, State.INACTIVE, marketAccountStore);
```

`makerBuyTokenQuantityReceive` is never checked for `<= 0` here, unlike the structurally identical computation in the "taker > maker" branch a few lines later: [2](#0-1) 

which explicitly guards against a zero result with `if (makerBuyTokenQuantityReceive == 0) { ... returnSellTokenRemain(makerOrderCapsule); return; }`, refunding the maker's tokens instead of destroying them.

`MarketUtils.multiplyAndDivide` performs integer floor division and can legitimately return `0` when `makerSellRemainQuantity * makerBuyQuantity < makerSellQuantity`: [3](#0-2) 

Because the maker's order was already fully validated/funded via `MarketSellAssetActuator` when originally placed (funds already deducted from the maker's account and only tracked in `sellTokenQuantityRemain`), setting `makerOrderCapsule.setSellTokenQuantityRemain(0)` and marking it `INACTIVE` permanently forfeits the maker's remaining locked balance without crediting anything back via `addTrxOrToken`, since `makerBuyTokenQuantityReceive == 0` means the subsequent `addTrxOrToken(makerOrderCapsule, makerBuyTokenQuantityReceive)` call adds zero.

This mirrors the reported bug class exactly: a share/ratio formula (`amount * numerator / denominator`) that can round to `0`, feeding directly into a state transition that destroys the user's locked value without minting/returning anything, and only one of the code's parallel branches contains the safety check that prevents this loss.

### Impact Explanation
A maker who places a `MarketSellAssetContract` order (locking funds in `MarketAccountStore`/order remain fields) can have their entire remaining balance silently zeroed out and the order marked `INACTIVE` with no compensating credit, when a taker's order happens to exactly match the maker's remaining sell quantity under a price ratio (`makerBuyQuantity/makerSellQuantity`) small enough to floor-divide to zero. This is a direct, permanent freezing/loss of a legitimate user's on-chain locked asset balance triggered purely by two unprivileged order-placer transactions (maker + taker), reachable through the ordinary `MarketSellAssetContract` transaction type with no special privilege required.

### Likelihood Explanation
Triggering requires: (1) a maker order with a sell/buy quantity ratio such that a small `sellTokenQuantityRemain` yields `sellTokenQuantityRemain * buyQuantity / sellQuantity == 0` (achievable by placing/partially-filling an order with `buyQuantity` sufficiently smaller than `sellQuantity`, or by repeated partial fills reducing `sellTokenQuantityRemain` to a small residual), and (2) a taker order whose computed `takerBuyTokenQuantityRemain` exactly equals that residual `sellTokenQuantityRemain`, which an attacker fully controls since taker order sizing is also attacker-chosen input. Both conditions are computable off-chain in advance since order books and existing order remainders are public on-chain data, making this practically exploitable by a motivated attacker either against their own maker order (self-attack does not help) or, more importantly, against any pre-existing thinly-priced maker order left in the book by consuming it with a precisely sized taker order to zero it out for a rival, or accidentally triggered during normal dust-clearing trades.

### Recommendation
Add the same zero-check guard used in the "taker > maker" branch to the "taker == maker" branch: if `makerBuyTokenQuantityReceive == 0`, call `MarketUtils.returnSellTokenRemain(makerOrderCapsule, ...)` to refund the maker's remaining sell-token balance before marking the order `INACTIVE`, rather than unconditionally zeroing `sellTokenQuantityRemain`.

### Proof of Concept
1. Maker places a `MarketSellAssetContract` selling token A for token B with `sellTokenQuantity = 1_000_000`, `buyTokenQuantity = 1` (extreme low price), and via partial fills reduces `sellTokenQuantityRemain` to a small value `R` such that `R * 1 / 1_000_000 == 0` (any `R < 1_000_000`).
2. A taker places a `MarketSellAssetContract` in the opposite pair sized so that `takerBuyTokenQuantityRemain` (computed via `MarketUtils.multiplyAndDivide`) equals exactly `R`, hitting the "taker == maker" branch in `matchSingleOrder`.
3. `makerBuyTokenQuantityReceive = multiplyAndDivide(R, 1, 1_000_000, ...) == 0`.
4. Code proceeds to `makerOrderCapsule.setSellTokenQuantityRemain(0)` and `updateOrderState(..., INACTIVE, ...)` without the zero-check present in the "taker > maker" branch, and `addTrxOrToken(makerOrderCapsule, 0)` credits nothing to the maker.
5. Maker's remaining `R` units of token A (already debited/locked at order placement) are permanently lost with no compensating credit.

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
