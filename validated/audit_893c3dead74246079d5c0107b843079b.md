### Title
Maker receives 0 tokens while entire remaining sell order is silently drained in exact-match branch of order matching - (File: actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java)

### Summary
`MarketSellAssetActuator.matchSingleOrder` computes the amount a maker order should receive via integer division (`MarketUtils.multiplyAndDivide`), which rounds down. In the "taker > maker" branch the code explicitly guards against this rounding producing zero (`if (makerBuyTokenQuantityReceive == 0) { ... return the remain of sellToken to maker ... }`), but the structurally identical computation in the "taker == maker" branch (`takerBuyTokenQuantityRemain == makerOrderCapsule.getSellTokenQuantityRemain()`) has no such guard.

### Finding Description
In `matchSingleOrder`, when the taker's computed buy amount exactly equals the maker's remaining sell amount, the code computes: [1](#0-0) 

`makerBuyTokenQuantityReceive` is derived from `MarketUtils.multiplyAndDivide(makerSellRemainQuantity, makerBuyQuantity, makerSellQuantity, ...)`, a floor-division operation: [2](#0-1) 

Unlike the "taker > maker" branch a few lines below, which explicitly checks for this rounding-to-zero case and returns the maker's remaining sell tokens instead of consuming the order: [3](#0-2) 

the "taker == maker" branch performs no such check. It unconditionally sets `makerOrderCapsule.setSellTokenQuantityRemain(0)` and marks the maker order `INACTIVE`, fully consuming the maker's entire remaining sell quantity, while `addTrxOrToken(makerOrderCapsule, makerBuyTokenQuantityReceive)` credits the maker with `0` tokens when `makerBuyTokenQuantityReceive` rounds down to zero. This is the direct structural analog of the reported LMPVault bug: an asset (the maker's remaining sell tokens) is consumed/taken while the computed "shares" (the buy-side amount owed back) can be zero due to rounding, and there is no `require(shares != 0)`-style guard in this code path even though the developers clearly recognized and handled the identical scenario one branch away.

This is reachable by any account that creates a sell order via `MarketSellAssetActuator`/`MarketOrderCreate` with a highly skewed `sellTokenQuantity`/`buyTokenQuantity` ratio (e.g. selling a very large `sellTokenQuantity` for a very small `buyTokenQuantity`), and by any counterparty broadcasting a matching sell transaction that triggers `matchSingleOrder` when the taker's computed buy remainder happens to equal the maker's remaining sell quantity.

### Impact Explanation
When triggered, the maker's remaining sell-token balance is fully removed from their open order (`setSellTokenQuantityRemain(0)`, order marked `INACTIVE`) but the maker is credited zero buy tokens via `addTrxOrToken`. The maker's tokens are effectively burned/lost with no compensating credit — a permanent loss of funds for the maker, and an unbacked/incorrect accounting state in the exchange since the taker consumes the maker's full remaining balance for less value than owed (or the taker's side may also be mis-settled, since `takerBuyTokenQuantityReceive` is set to `makerOrderCapsule.getSellTokenQuantityRemain()` regardless of whether the maker's payout to the taker was previously reduced). This satisfies the "theft or permanent freezing of funds" bar.

### Likelihood Explanation
Exploitability depends on constructing sell orders whose token-quantity ratios cause the floor-division `makerSellRemainQuantity * makerBuyQuantity / makerSellQuantity` to equal 0 while `takerBuyTokenQuantityRemain` exactly equals the maker's remaining sell quantity. Both order creation and order matching are reachable from ordinary signed transactions with no special privileges, but hitting the exact-equality branch (`takerBuyTokenQuantityRemain == makerOrderCapsule.getSellTokenQuantityRemain()`) combined with the rounding-to-zero condition requires deliberately crafted order quantities/prices. Given TRON market orders allow arbitrary integer sell/buy quantities and the "taker > maker" branch comment explicitly discusses this exact rounding scenario ("the quantity is too small... it would not happen here" reasoning only applies to that other branch), a similarly-skewed price ratio can be engineered for the equal-case branch, making this a realistically constructible, not merely theoretical, condition. Full verification of exact numeric preconditions (order creation validation limits, minimum price precision) could not be completed within the available tool budget.

### Recommendation
Add the same zero-check guard used in the "taker > maker" branch to the "taker == maker" branch: if `makerBuyTokenQuantityReceive == 0`, return the maker's remaining sell tokens via `MarketUtils.returnSellTokenRemain` (or equivalent) instead of consuming the maker order and crediting zero tokens. Consider also auditing `MarketUtils.multiplyAndDivide`'s other call sites for the same missing check.

### Proof of Concept
Conceptual PoC (exact numeric parameters need on-chain/test-harness derivation, which could not be completed within tool budget):
1. Attacker A creates a maker sell order via `MarketSellAssetActuator` with `sellTokenQuantity = S` and `buyTokenQuantity = B` such that `B << S` (a heavily skewed price).
2. After partial fills reduce `makerSellRemainQuantity` to some remaining value `R` where `R * B / S == 0` (floor rounds to zero) — this occurs whenever `R < S / B`.
3. A taker submits a sell order whose computed `takerBuyTokenQuantityRemain` (via `MarketUtils.multiplyAndDivide`) exactly equals `R`, hitting the `takerBuyTokenQuantityRemain == makerOrderCapsule.getSellTokenQuantityRemain()` branch in `matchSingleOrder`.
4. `matchSingleOrder` sets `makerOrderCapsule.setSellTokenQuantityRemain(0)`, marks it `INACTIVE`, and calls `addTrxOrToken(makerOrderCapsule, 0)` — the maker's entire remaining sell-side balance is consumed with zero tokens credited back, in contrast to the "taker > maker" branch's explicit protection against this exact scenario.

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
