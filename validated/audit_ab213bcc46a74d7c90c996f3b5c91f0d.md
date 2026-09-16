### Title
Maker order's remaining sell tokens can be taken for free in `MarketSellAssetActuator` due to unguarded rounding-to-zero in `matchSingleOrder` - (File: actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java)

### Summary
`MarketSellAssetActuator.matchSingleOrder` computes how many tokens a maker receives from a matched trade using integer division (`MarketUtils.multiplyAndDivide`). In the "taker == maker" branch this result is used unconditionally, even when it rounds down to zero, while the taker is still credited with the maker's *entire* remaining sell-token balance. This lets any order placer drain the final remainder of another user's resting sell order without paying anything, exactly the same rounding-to-zero root cause described in the external report (a division whose numerator is smaller than the denominator floors to `0`, allowing the payer to skip payment while still receiving the asset).

### Finding Description
In `matchSingleOrder`, the taker's desired purchase amount is computed first: [1](#0-0) 

When `takerBuyTokenQuantityRemain` exactly equals the maker's remaining sell quantity, the code enters the "taker == maker" branch and computes what the *maker* should receive via a second, independent division: [2](#0-1) 

`makerBuyTokenQuantityReceive = floor(makerSellRemainQuantity * makerBuyQuantity / makerSellQuantity)`. This is a different ratio/rounding operation than the one used to derive `takerBuyTokenQuantityRemain`, so it can independently round down to `0` even though `makerSellRemainQuantity > 0` was already established. Despite this, the code:
- sets `makerOrderCapsule.setSellTokenQuantityRemain(0)` unconditionally (maker's order is fully consumed / marked `INACTIVE`), and
- sets `takerBuyTokenQuantityReceive = makerOrderCapsule.getSellTokenQuantityRemain()` (the *pre*-zeroed remainder) — i.e., the taker is credited with the maker's *full* remaining sell tokens via `addTrxOrToken`.

Contrast this with the sibling "taker > maker" branch a few lines below, which performs the same kind of division and explicitly guards against the zero-result case before consuming the maker's remaining balance: [3](#0-2) 

That branch returns the maker's remaining sell tokens back to the maker if `makerBuyTokenQuantityReceive == 0` rather than letting the taker take them. The "taker == maker" branch has no such guard, so the maker can be fully consumed while receiving `0` payment tokens.

Both `takerBuyTokenQuantityReceive` and `makerBuyTokenQuantityReceive` are subsequently credited to accounts unconditionally: [4](#0-3) 

### Impact Explanation
This lets any anonymous order-placer (submitting a `MarketSellAssetContract` transaction) craft or find a taker sell order whose computed remaining-buy quantity exactly matches the remainder of *any* resting maker order in the order book, when that maker's remainder is small enough (a common situation as an order approaches full fill) that `remain * buyQty / sellQty` floors to zero. The victim maker's order is marked fully filled/`INACTIVE` and loses its remaining asset balance while the attacker (taker) receives that balance in full for zero payment. This is a direct theft-of-funds vulnerability on the TRC10 market exchange, matching the "High" severity of the analogous report (unbacked transfer/theft of tokens due to a rounding-to-zero division check that exists in one code path but is missing in a sibling code path).

### Likelihood Explanation
The trigger condition — a maker's remaining sell quantity being small relative to its price ratio — occurs naturally whenever a resting order is nearly fully filled by prior partial matches, which is a normal, expected state in any active order book. An attacker only needs to submit a correctly sized taker sell order (fully within their control, no special privileges) to trigger the exact-match branch against a chosen victim maker order. No validator collusion, no special permissions, and no race condition beyond normal block-inclusion ordering are required — a single crafted transaction from an unprivileged order placer is sufficient.

### Recommendation
Add the same zero-result guard used in the "taker > maker" branch to the "taker == maker" branch: if `makerBuyTokenQuantityReceive == 0` after `MarketUtils.multiplyAndDivide`, return the maker's remaining sell tokens to the maker (via `MarketUtils.returnSellTokenRemain`) and mark the order inactive/return, instead of letting the taker consume the maker's tokens for free.

### Proof of Concept
1. Maker `M` creates a `MarketSellAssetContract` order selling `sellQty` of token A for `buyQty` of token B (or TRX), e.g. sell `1000` A for `1` B.
2. Through prior normal partial matches (or a crafted first trade), the maker's order is left with `makerSellRemainQuantity = R` where `R * buyQty / sellQty < 1` (e.g., `R = 500`, `buyQty = 1`, `sellQty = 1000` → `500*1/1000 = 0`), but `R > 0`.
3. Attacker `T` submits a `MarketSellAssetContract` (taker) order whose `sellTokenQuantity`/`buyTokenQuantity` are chosen so that the computed `takerBuyTokenQuantityRemain` (line 402-404) exactly equals `R`.
4. `matchSingleOrder` enters the "taker == maker" branch: `makerBuyTokenQuantityReceive` computes to `0`; the maker's `sellTokenQuantityRemain` is set to `0` and the order is closed; the taker is credited with `takerBuyTokenQuantityReceive = R` (the maker's full remainder) via `addTrxOrToken`, while the maker receives `0` tokens back for it.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L402-413)
```java
    long takerBuyTokenQuantityRemain = MarketUtils
        .multiplyAndDivide(takerSellRemainQuantity, makerSellQuantity, makerBuyQuantity,
            this.disableJavaLangMath());

    if (takerBuyTokenQuantityRemain == 0) {
      // quantity too small, return sellToken to user
      takerOrderCapsule.setSellTokenQuantityReturn();
      MarketUtils.returnSellTokenRemain(takerOrderCapsule, takerAccountCapsule,
          dynamicStore, assetIssueStore);
      MarketUtils.updateOrderState(takerOrderCapsule, State.INACTIVE, marketAccountStore);
      return;
    }
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

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L488-490)
```java
    // add token into account
    addTrxOrToken(takerOrderCapsule, takerBuyTokenQuantityReceive, takerAccountCapsule);
    addTrxOrToken(makerOrderCapsule, makerBuyTokenQuantityReceive);
```
