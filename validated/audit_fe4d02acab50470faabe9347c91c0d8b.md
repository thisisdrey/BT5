### Title
Market order matching overpays counterparties due to floor-division rounding when an order is fully exhausted - ([File: actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java])

### Summary
`MarketSellAssetActuator.matchSingleOrder()` computes matched quantities with an integer floor division (`MarketUtils.multiplyAndDivide`), then transfers the **entire remaining balance** of whichever order (taker or maker) is exhausted in that match, instead of transferring only the exact amount required to obtain the rounded-down counter-quantity. This is the same class of bug described in the Gumball report: the full input amount is taken from the user even though a smaller amount was mathematically sufficient, and the rounding remainder is silently given away to the counterparty rather than being preserved for/returned to its owner.

### Finding Description
In `matchSingleOrder()`, the quantity the taker can buy is computed with a floor division: [1](#0-0) 

When the taker's order is the smaller one ("taker < maker"), the taker's *entire* remaining sell balance is handed to the maker, while the taker only receives the floor-rounded buy quantity: [2](#0-1) 

The symmetric case ("taker > maker") does the same thing in the opposite direction — the maker's *entire* remaining sell balance is transferred to the taker, while the maker only receives the floor-rounded buy amount computed via the same truncating division: [3](#0-2) 

The underlying division utility always rounds down and never computes/returns the "exact minimal input needed" for the rounded output: [4](#0-3) 

Mathematically: if `A = floor(B_full * mS / mB)` is the matched buy quantity, then the *exact* sell amount required to obtain `A` is `ceil(A * mB / mS)`, which is provably `<= B_full`. Whenever it is strictly less than `B_full`, the difference (`B_full - ceil(A*mB/mS)`) is dust that is silently transferred to the counterparty for free instead of being preserved in the exhausted order's `SellTokenQuantityRemain` (which would allow it to be matched further or returned to the owner on cancellation, as is correctly done elsewhere in the same function when the computed quantity rounds to `0`, cf. `takerOrderCapsule.setSellTokenQuantityReturn()` path).

Numeric example: maker order sells 100 A for 201 B (price 2.01 B/A). Taker has `B_full = 6` remaining. `A = floor(6*100/201) = 2`. The exact B needed to buy 2 A at this price is `ceil(2*201/100) = 5`. But the code sets `makerBuyTokenQuantityReceive = takerOrderCapsule.getSellTokenQuantityRemain() = 6` (the taker's full remaining balance), transferring 1 extra unit of B to the maker that the taker never should have had to pay.

### Impact Explanation
Every partial match where an order is exhausted (which is the common case in order-book matching, not an edge case) can leak a small amount of the exhausted party's asset to the counterparty. This is triggerable by any account submitting `MarketSellAssetContract` transactions (`MarketSellAssetActuator`, reachable directly from `Wallet`/gRPC/HTTP `createMarketSellAssetTransaction`), with no special privilege required. While each individual leak is bounded by a fraction of one token unit, it occurs on essentially every partially-filled match, is systematic (loss is unidirectional, always benefiting the non-exhausted side), and constitutes an unauthorized/uncompensated transfer of user funds — a permanent, protocol-level loss of value rather than a display or precision quirk.

### Likelihood Explanation
High likelihood of occurrence in normal usage: any two orders with sell/buy quantities whose ratio does not divide evenly relative to the exhausted side's remaining balance will trigger the truncation. No adversarial setup is needed — ordinary trading activity with arbitrary price ratios will regularly produce non-exact divisions. An attacker (as a maker) could also deliberately post orders with price ratios chosen to maximize the "overpayment" dust extracted from unsuspecting takers across many small matches.

### Recommendation
When one side of a match is fully exhausted, do not blindly transfer that side's entire remaining balance. Instead compute the exact amount required to justify the rounded-down counter-quantity (e.g., `ceil(A * mB / mS)`) and only transfer that amount, leaving any true remainder in the order's `SellTokenQuantityRemain` for further matching or eventual return to the owner — mirroring the existing handling used when a computed match quantity rounds to zero.

### Proof of Concept
1. Account M places a sell order: sell 100 of token A for 201 of token B (`addOrder`/`MarketSellAssetContract`).
2. Account T places a sell order: sell 6 of token B for token A, matching against M's order (`MarketSellAssetContract`).
3. In `matchSingleOrder`, `takerBuyTokenQuantityRemain = floor(6*100/201) = 2` (line 402-404), which is less than maker's remaining sell quantity (100), so the "taker < maker" branch executes (lines 440-452).
4. `makerBuyTokenQuantityReceive` is set to `takerOrderCapsule.getSellTokenQuantityRemain()` = 6 (T's full remaining B), while T only receives 2 A.
5. The exact B required for 2 A at the posted price is `ceil(2*201/100) = 5`. T has therefore paid 1 unit of B more than mathematically required, which is credited entirely to M with no corresponding extra A returned to T — a permanent, un-backed 1-unit loss for T.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L402-404)
```java
    long takerBuyTokenQuantityRemain = MarketUtils
        .multiplyAndDivide(takerSellRemainQuantity, makerSellQuantity, makerBuyQuantity,
            this.disableJavaLangMath());
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L440-452)
```java
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
