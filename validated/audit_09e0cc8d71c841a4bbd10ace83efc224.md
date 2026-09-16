## Analysis: Rounding-to-zero fee/quantity bug in TRON Market Order matching

The bug-class described in the report — integer division truncating a computed amount to zero, causing funds to become stuck — has a directly reachable analog in java-tron's on-chain order-book matching logic used by `MarketSellAssetActuator`, which any account can trigger via a `MarketSellAssetContract` transaction (an order placer / anonymous transaction broadcaster).

### Title
Maker's sell tokens can be fully consumed with zero tokens received due to unguarded rounding-to-zero division in market order matching - (File: `actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java`)

### Summary
In `matchSingleOrder`, the taker-equals-maker branch computes the amount the maker receives via floor-division (`MarketUtils.multiplyAndDivide`), but — unlike the sibling "taker > maker" branch — it never checks whether that computed amount rounds down to zero before permanently closing the maker's order and marking their entire remaining sell-token balance as consumed.

### Finding Description
`matchSingleOrder` has three branches depending on how `takerBuyTokenQuantityRemain` compares to `makerOrderCapsule.getSellTokenQuantityRemain()`. In the "taker == maker" branch: [1](#0-0) 

`makerBuyTokenQuantityReceive` is computed via `MarketUtils.multiplyAndDivide(makerSellRemainQuantity, makerBuyQuantity, makerSellQuantity, ...)`, which performs a floor division: [2](#0-1) 

Immediately afterward, `makerOrderCapsule.setSellTokenQuantityRemain(0)` unconditionally zeroes out and inactivates the maker's order — regardless of whether `makerBuyTokenQuantityReceive` computed to `0`. The maker's `sellTokenQuantityRemain` (already transferred out of their account when the order was created via `transferBalanceOrToken`) is thus fully consumed by the taker, while `addTrxOrToken(makerOrderCapsule, makerBuyTokenQuantityReceive)` credits the maker with `0` of the buy token: [3](#0-2) 

Contrast this with the "taker > maker" branch, which performs the identical division but explicitly guards against a zero result and refunds the maker's remaining sell tokens instead of silently zeroing them out: [4](#0-3) 

The code comment on that guarded branch even acknowledges the assumption is fragile ("it would not happen here... when sellQuantity < buyQuantity, it will get at least one buyToken even when sellRemain = 1"), but this invariant is never enforced, and the "taker == maker" branch has no such protection at all, no matter the sell/buy quantity ratio.

### Impact Explanation
When `makerSellRemainQuantity * makerBuyQuantity / makerSellQuantity` rounds down to `0` (which happens whenever `makerBuyQuantity < makerSellQuantity` and the remaining sell quantity is small relative to the ratio — exactly the "small decimal token" / low-value-per-unit scenario from the report), the maker's order is fully closed and their sell-side balance is permanently consumed with zero compensation credited. This is a concrete case of permanent loss/freezing of a maker's funds triggered purely by a taker placing a matching sell order with an adversarially-chosen quantity.

### Likelihood Explanation
This is trivially triggerable by any two colluding or independent accounts: place a maker sell order with a sell/buy ratio where `makerBuyQuantity < makerSellQuantity`, then place a taker order whose `takerBuyTokenQuantityRemain` exactly equals the maker's remaining sell quantity but such that the reverse-computed `makerBuyTokenQuantityReceive` truncates to zero. No special privileges, precompiles, or off-chain conditions are required — only ordinary `MarketSellAssetContract` broadcasts.

### Recommendation
Add the same zero-check-and-refund guard used in the "taker > maker" branch to the "taker == maker" branch: if `makerBuyTokenQuantityReceive == 0`, call `makerOrderCapsule.setSellTokenQuantityReturn()` / `returnSellTokenRemain(makerOrderCapsule)` instead of unconditionally zeroing the maker's remaining sell quantity and marking the order `INACTIVE`.

### Proof of Concept
1. Maker places a sell order: `sellTokenQuantity = 1000`, `buyTokenQuantity = 1` (sell/buy ratio 1000:1).
2. Order partially fills over time until `sellTokenQuantityRemain` for the maker equals some small value `R` (e.g., `R = 500`) such that `R * 1 / 1000 == 0` (floor division).
3. Taker places a sell order whose computed `takerBuyTokenQuantityRemain` (via `MarketUtils.multiplyAndDivide`) exactly equals `R`, hitting the "taker == maker" branch.
4. `matchSingleOrder` sets `makerOrderCapsule.setSellTokenQuantityRemain(0)` and marks it `INACTIVE`, while `addTrxOrToken(makerOrderCapsule, 0)` credits the maker with zero buy tokens — the maker's remaining `R` sell tokens (already debited from their account when the order was placed) are irrecoverably lost.

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

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L453-483)
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
    }
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
