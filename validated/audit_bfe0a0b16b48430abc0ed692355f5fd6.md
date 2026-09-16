I found a clear analog. In `MarketSellAssetActuator.matchSingleOrder()`, the order-matching logic uses integer division that can round the maker's received amount to zero — but only one of the three branches checks for this and handles it safely. The equal-quantity branch is missing the check.### Title
Market order matching lets a taker steal a maker's tokens for free when the exact-match branch rounds the maker's proceeds to zero - (File: actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java)

### Summary
`MarketSellAssetActuator.matchSingleOrder()` computes the amount a maker receives from a trade using integer division (`MarketUtils.multiplyAndDivide`), which can legitimately round down to `0` when a maker order has an extreme sell/buy quantity ratio and only a small remaining quantity left. The "taker > maker" branch explicitly guards against this rounding-to-zero case and safely returns the dust back to the maker, but the "taker == maker" (exact match) branch performs the identical computation with no such guard, so the maker's entire remaining sell-token balance can be consumed while `makerBuyTokenQuantityReceive` is `0`.

### Finding Description
In `matchSingleOrder`, the taker's target amount is computed as: [1](#0-0) 

If the two remaining quantities happen to match exactly, the code enters the "taker == maker" branch: [2](#0-1) 

`makerBuyTokenQuantityReceive` is calculated via `MarketUtils.multiplyAndDivide(makerSellRemainQuantity, makerBuyQuantity, makerSellQuantity, ...)`, which performs `floorDiv(makerSellRemainQuantity * makerBuyQuantity, makerSellQuantity)`: [3](#0-2) 

When the maker order has an extreme price ratio (very large `makerSellQuantity` relative to `makerBuyQuantity`) and only a small `makerSellRemainQuantity` is left (e.g. dust after prior partial fills), this division truncates to `0`. Unlike the "taker > maker" branch, which explicitly checks `if (makerBuyTokenQuantityReceive == 0)` and returns the sell-token remainder to the maker instead of completing the trade: [4](#0-3) 

the exact-match branch has no equivalent check. It unconditionally sets `makerOrderCapsule.setSellTokenQuantityRemain(0)`, marks the maker order `INACTIVE`, and lets execution fall through to: [5](#0-4) 

which credits the taker with `takerBuyTokenQuantityReceive` (the maker's full remaining sell tokens) while crediting the maker with `makerBuyTokenQuantityReceive == 0`.

### Impact Explanation
This is a direct, unauthorized transfer of the maker's asset balance to the taker with no compensation, achievable through the standard `MarketSellAssetContract` order-placement path (`MarketSellAssetActuator.execute` → `matchOrder` → `matchSingleOrder`) that any account can reach by placing sell orders. A maker order that has been partially filled down to a small remaining quantity, combined with a taker order sized to exactly consume that remainder, silently transfers the maker's remaining tokens to the taker for zero payment — theft of user funds.

### Likelihood Explanation
Any account can create maker orders with arbitrary `sellTokenQuantity`/`buyTokenQuantity` ratios (no minimum price-ratio constraint is enforced in validation), and orders naturally get partially filled down to small remainders through normal trading. An attacker (or their own paired accounts) can engineer a maker order with an extreme ratio and craft a taker order whose remaining sell quantity exactly equals the maker's dust remainder, deterministically triggering the "taker == maker" branch and the zero-rounding condition. This requires only standard, permissionless `MarketSellAssetContract` transactions and no special privileges.

### Recommendation
Add the same zero-amount guard used in the "taker > maker" branch to the "taker == maker" branch: if `makerBuyTokenQuantityReceive == 0`, return the maker's remaining sell tokens back to the maker (via `MarketUtils.returnSellTokenRemain`) instead of zeroing out the maker's remaining balance and crediting the taker for free.

### Proof of Concept
1. Attacker (or colluding account) places maker order: sell 1,000,000,000 of token A, buy 1 of token B (`sellTokenQuantity=1e9`, `buyTokenQuantity=1`).
2. Through normal or attacker-controlled partial fills, the maker order's `sellTokenQuantityRemain` is reduced to a small dust value, e.g. `makerSellRemainQuantity = 1` (with `makerSellQuantity=1e9`, `makerBuyQuantity=1` still fixed on the original order).
3. Attacker submits a taker order sized so that `takerBuyTokenQuantityRemain` (computed at line 402-404) equals exactly `1` (the maker's remaining sell quantity), entering the "taker == maker" branch at line 418.
4. `makerBuyTokenQuantityReceive = floorDiv(1 * 1, 1e9) = 0` (line 426-428).
5. The code proceeds to set `makerOrderCapsule.setSellTokenQuantityRemain(0)` (line 434) and mark the maker order `INACTIVE` (line 439), then at line 489-490 credits the taker with `takerBuyTokenQuantityReceive = 1` unit of token A while crediting the maker with `0` units of token B — the maker's final token is taken for free.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L402-404)
```java
    long takerBuyTokenQuantityRemain = MarketUtils
        .multiplyAndDivide(takerSellRemainQuantity, makerSellQuantity, makerBuyQuantity,
            this.disableJavaLangMath());
```

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

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L485-490)
```java
    // save makerOrderCapsule
    orderStore.put(makerOrderCapsule.getID().toByteArray(), makerOrderCapsule);

    // add token into account
    addTrxOrToken(takerOrderCapsule, takerBuyTokenQuantityReceive, takerAccountCapsule);
    addTrxOrToken(makerOrderCapsule, makerBuyTokenQuantityReceive);
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
