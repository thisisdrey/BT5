### Title
Missing zero-quantity check in the "taker == maker" match branch of `MarketSellAssetActuator.matchSingleOrder` lets a trader drain a maker's remaining tokens for free - (File: actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java)

### Summary
`MarketSellAssetActuator.matchSingleOrder` computes the amount a maker receives via integer division (`MarketUtils.multiplyAndDivide`), which rounds down to zero when the maker's remaining sell quantity is small relative to its price ratio. Two of the three match branches ("taker < maker" implicitly via the initial `takerBuyTokenQuantityRemain == 0` guard, and explicitly "taker > maker") guard against this rounding-to-zero case and return the tokens to the order owner. The "taker == maker" branch does not perform this check, so a maker's full remaining sell quantity can be transferred to the taker while the maker receives `0` compensation, and the taker's own sell balance is left untouched.

### Finding Description
In `matchSingleOrder`, three branches decide how a match between a taker order and a maker order is resolved [1](#0-0) :

1. `takerBuyTokenQuantityRemain == 0`: explicitly guarded before branching, returns the taker's own tokens if the computed amount rounds to zero [2](#0-1) .
2. "taker > maker": `makerBuyTokenQuantityReceive` is computed with the same rounding-down `multiplyAndDivide` call, and is explicitly checked for `== 0`, returning the sell-token remainder to the maker if so [3](#0-2) .
3. "taker == maker" (`takerBuyTokenQuantityRemain == makerOrderCapsule.getSellTokenQuantityRemain()`): `makerBuyTokenQuantityReceive` is computed identically via `multiplyAndDivide(makerSellRemainQuantity, makerBuyQuantity, makerSellQuantity, ...)`, but **no zero-check is performed**. The maker's `SellTokenQuantityRemain` is unconditionally set to `0` (order fully consumed / INACTIVE), while the taker unconditionally receives `takerBuyTokenQuantityReceive = makerOrderCapsule.getSellTokenQuantityRemain()` in full [4](#0-3) .

If `makerBuyTokenQuantityReceive` rounds down to `0`, then:
- `takerSellTokenLeft = takerOrderCapsule.getSellTokenQuantityRemain() - 0` — the taker's sell balance is left completely unspent.
- `addTrxOrToken(makerOrderCapsule, 0)` credits the maker with nothing while their order is closed with `SellTokenQuantityRemain = 0` [5](#0-4) .
- `addTrxOrToken(takerOrderCapsule, takerBuyTokenQuantityReceive, ...)` credits the taker with the maker's full remaining tokens for free.

The rounding helper used for these computations is `MarketUtils.multiplyAndDivide`, which performs floor division (`floorDiv`) with no minimum-result enforcement [6](#0-5) . This is analogous to the reported Opyn `CrabNetting` issue, where `(quantity * clearingPrice) / 1e18` rounds to zero, letting a trader receive tokens without paying — except here the affected party is not the caller but a third-party maker order sitting in the order book, and the vulnerability is directly reachable from a normal `MarketSellAssetContract` transaction placed by any account (order placer persona).

### Impact Explanation
An attacker can craft (or wait for) a maker order whose remaining sell quantity is small enough that `remain * buyQuantity / sellQuantity` rounds to `0`, then submit a matching taker order sized so `takerBuyTokenQuantityRemain` exactly equals that remainder. The maker's tokens are removed from their order and credited to the taker for zero payment, while the taker's own paying-side balance is untouched. This is a concrete theft of the maker's assets / unbacked token creation for the taker, directly reachable by any unprivileged account through the `MarketSellAssetContract` transaction path.

### Likelihood Explanation
Any account can create both the maker order (via `MarketSellAssetContract`) and drive it down to a small remainder through self-trading or normal partial fills, then submit the crafted taker order in a second `MarketSellAssetContract` transaction to trigger the exact-match branch. No special privileges are required — only two ordinary signed transactions from the market order-placement API, matching the "order placer" persona explicitly in scope.

### Recommendation
Add the same zero-result guard used in the "taker > maker" branch to the "taker == maker" branch: if `makerBuyTokenQuantityReceive == 0` after `MarketUtils.multiplyAndDivide`, return the maker's remaining sell tokens to the maker (`MarketUtils.returnSellTokenRemain`) and mark the order state accordingly instead of unconditionally zeroing `SellTokenQuantityRemain` and crediting the taker.

### Proof of Concept
1. Maker places a sell order via `MarketSellAssetContract` with `sellTokenQuantity = 250`, `buyTokenQuantity = 100` (ratio 2.5:1), and through prior matching reduces `SellTokenQuantityRemain` to `R = 2`.
2. Taker (attacker) places a sell order via `MarketSellAssetContract` in the reverse pair with `takerSellRemainQuantity = T = 1`.
3. In `matchSingleOrder`: `takerBuyTokenQuantityRemain = multiplyAndDivide(1, 250, 100) = floor(2.5) = 2`, which equals `makerOrderCapsule.getSellTokenQuantityRemain() = 2` → enters the "taker == maker" branch [7](#0-6) .
4. `makerBuyTokenQuantityReceive = multiplyAndDivide(2, 100, 250) = floor(0.8) = 0`.
5. `takerBuyTokenQuantityReceive = 2` (full maker remainder); `takerSellTokenLeft = 1 - 0 = 1` (taker's sell balance untouched).
6. Maker order is set to `SellTokenQuantityRemain = 0` / `INACTIVE` and receives `0` tokens; taker receives `2` tokens for free and keeps their full `1` unit sell balance for future trades.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L406-413)
```java
    if (takerBuyTokenQuantityRemain == 0) {
      // quantity too small, return sellToken to user
      takerOrderCapsule.setSellTokenQuantityReturn();
      MarketUtils.returnSellTokenRemain(takerOrderCapsule, takerAccountCapsule,
          dynamicStore, assetIssueStore);
      MarketUtils.updateOrderState(takerOrderCapsule, State.INACTIVE, marketAccountStore);
      return;
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L415-439)
```java
    long takerBuyTokenQuantityReceive; // In this match, the token obtained by taker
    long makerBuyTokenQuantityReceive; // the token obtained by maker

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

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L489-490)
```java
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
