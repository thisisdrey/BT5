## Finding

### Title
Maker orders can lose all remaining sell tokens for zero payment due to unchecked rounding-to-zero in `MarketSellAssetActuator.matchSingleOrder` - (File: `actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java`)

### Summary
`MarketSellAssetActuator.matchSingleOrder()` computes the amount a maker receives (`makerBuyTokenQuantityReceive`) using an integer division (`MarketUtils.multiplyAndDivide`). In the "taker == maker" branch this result is used to reduce the taker's remaining balance and is credited to the maker, but unlike the sibling "taker > maker" branch, this branch has **no check that the computed amount is greater than zero** before fully closing the maker's order and zeroing its remaining sell-token balance.

### Finding Description
In `matchSingleOrder`, when `takerBuyTokenQuantityRemain == makerOrderCapsule.getSellTokenQuantityRemain()` (the "taker == maker" case), the code computes: [1](#0-0) 

Specifically:
```
makerBuyTokenQuantityReceive = MarketUtils.multiplyAndDivide(makerSellRemainQuantity, makerBuyQuantity, makerSellQuantity, ...);
takerBuyTokenQuantityReceive = makerOrderCapsule.getSellTokenQuantityRemain();
long takerSellTokenLeft = takerOrderCapsule.getSellTokenQuantityRemain() - makerBuyTokenQuantityReceive;
takerOrderCapsule.setSellTokenQuantityRemain(takerSellTokenLeft);
makerOrderCapsule.setSellTokenQuantityRemain(0);
```
`MarketUtils.multiplyAndDivide` performs `floorDiv(a*b, c)`, which rounds down and can legitimately return `0` when `makerSellRemainQuantity * makerBuyQuantity < makerSellQuantity`: [2](#0-1) 

Compare this with the "taker > maker" branch just below, which explicitly guards against a zero result and refunds the maker instead of destroying its balance: [3](#0-2) 

The comment at lines 466-474 even reasons that this situation "would not happen here" for the taker>maker branch based on an assumed relationship between quantities — but no equivalent reasoning or protection exists for the "taker == maker" branch, where `makerBuyTokenQuantityReceive` can independently round to `0` while the maker's order is unconditionally closed via `setSellTokenQuantityRemain(0)` and marked `INACTIVE`.

The result: the maker's full remaining sell-token balance is consumed and the order closed, `addTrxOrToken(makerOrderCapsule, makerBuyTokenQuantityReceive)` credits the maker `0` tokens, while the taker still receives `takerBuyTokenQuantityReceive = makerOrderCapsule.getSellTokenQuantityRemain()` (the maker's full amount) and only pays a reduced/rounded `makerBuyTokenQuantityReceive` — i.e., pays 0.

### Impact Explanation
This is a direct, unauthorized loss of user funds analogous to the reported `repay()` bug class: a state-changing transfer occurs based on a share/quantity computation that can silently round to zero, deducting/consuming the victim's full balance without crediting the corresponding proceeds. Any account that places a resting sell order (`MarketSellAssetContract`) via `MarketSellAssetActuator` can have their entire remaining order balance drained for free by a taker who crafts an order whose remaining amount satisfies `makerSellRemainQuantity * makerBuyQuantity < makerSellQuantity`.

### Likelihood Explanation
Reachable via a single unprivileged `MarketSellAssetContract` transaction (any account can create sell/buy orders on the on-chain exchange market). `validate()` only enforces `sellTokenQuantity > 0` and `buyTokenQuantity > 0` for the taker's own order and a market-wide quantity limit; it performs no check preventing a maker's remaining balance/price ratio from producing a floor-division result of zero in the taker==maker branch. [4](#0-3) 
An attacker can engineer a maker order (their own, or wait for any thin resting order) with a low sell-remain/buy-quantity ratio relative to sell-quantity and then submit a matching taker order sized to trigger the exact-equality branch.

### Recommendation
Add the same zero-check used in the "taker > maker" branch to the "taker == maker" branch: if `makerBuyTokenQuantityReceive == 0`, do not close/zero the maker's order and drain its full remaining balance; instead return/refund the maker's remaining sell tokens (as done via `returnSellTokenRemain`) rather than crediting it with a swallowed zero-value trade.

### Proof of Concept
1. Attacker (or colluding account) creates a maker sell order via `MarketSellAssetContract` with `sellTokenQuantity = S`, `buyTokenQuantity = B` such that the pool ratio yields a small `makerSellRemainQuantity` at some point (e.g., after partial fills leave `makerSellRemainQuantity = R`).
2. A taker submits a `MarketSellAssetContract` order sized so that `takerBuyTokenQuantityRemain` computed in `matchSingleOrder` (line 402-404 of `MarketSellAssetActuator.java`) exactly equals `makerOrderCapsule.getSellTokenQuantityRemain()` (`R`), and such that `R * B / S == 0` under floor division (i.e., `R * B < S`).
3. `matchSingleOrder` enters the `takerBuyTokenQuantityRemain == makerOrderCapsule.getSellTokenQuantityRemain()` branch, computes `makerBuyTokenQuantityReceive = 0`, sets `makerOrderCapsule.setSellTokenQuantityRemain(0)` (closing/zeroing the maker's order) and credits the taker with `takerBuyTokenQuantityReceive = R` tokens via `addTrxOrToken`, while crediting the maker `0` via `addTrxOrToken(makerOrderCapsule, 0)`.
4. The maker's full remaining `R` sell tokens are gone from their order with zero tokens received in exchange — the taker walked away with `R` tokens for free.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L223-230)
```java
    if (sellTokenQuantity <= 0 || buyTokenQuantity <= 0) {
      throw new ContractValidateException("token quantity must greater than zero");
    }

    long quantityLimit = dynamicStore.getMarketQuantityLimit();
    if (sellTokenQuantity > quantityLimit || buyTokenQuantity > quantityLimit) {
      throw new ContractValidateException("token quantity must less than " + quantityLimit);
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
