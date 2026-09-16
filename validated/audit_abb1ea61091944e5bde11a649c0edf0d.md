### Title
Rounding-in-favor-of-taker in market order matching allows systematic underpayment of makers - ([File: actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java])

### Summary
`MarketSellAssetActuator.matchSingleOrder` computes the amount a taker must pay a maker using `MarketUtils.multiplyAndDivide`, which performs a floor (truncating) integer division. In the "taker > maker" branch, the taker receives the maker's **full** remaining sell-token balance, but only has the **floor-rounded** (and therefore possibly smaller) amount deducted from their own remaining balance. This is structurally identical to the reported Hedge Vault bug: an amount computed through division is rounded down before being applied to the paying side's liability/debit, while the receiving side gets the un-rounded, larger amount.

### Finding Description
`MarketUtils.multiplyAndDivide` truncates towards zero: [1](#0-0) 

In `matchSingleOrder`, when the taker's requested amount exceeds the maker's remaining sell quantity ("taker > maker" branch), the taker unconditionally receives the maker's entire `makerSellRemainQuantity`: [2](#0-1) 

But the amount debited from the taker's own remaining sell balance (`makerBuyTokenQuantityReceive`, i.e. what the maker is credited with) is the *floor* of `makerSellRemainQuantity * makerBuyQuantity / makerSellQuantity`. Whenever this division has a non-zero remainder, the taker is charged strictly less than the exact fair-value price for consuming the maker's full remaining order, while the maker's proceeds are correspondingly short-changed by the fractional remainder. The code even acknowledges rounding to `0` is anticipated (see the inline comment at lines 466-474 of `MarketSellAssetActuator.java`), but only guards against the exact-zero case — not the general underpayment.

Because a single `MarketSellAssetContract` transaction from any account can drive many maker-order matches in one call (bounded by `MAX_MATCH_NUM`), and because an attacker fully controls both the taker order parameters and can pre-seed cheap maker orders whose `sellTokenQuantity`/`buyTokenQuantity` ratio does not evenly divide the remaining quantity, the rounding loss can be forced and repeated across many transactions, analogous to the reported PoC that repeatedly issued "loan amount = 1" to accumulate value while liabilities never increased.

### Impact Explanation
Each truncation event lets the taker consume the maker's full remaining sell-side balance while paying less than the exact computed fair-value amount. Repeated over many small orders/matches, this results in a real transfer of value (TRX or TRC10 tokens) from makers to a taker without an equivalent debit — a form of fund theft reachable by any unprivileged account issuing `MarketSellAssetContract` transactions (an in-scope broadcastable contract type / order placer).

### Likelihood Explanation
The vulnerability is reachable by any account with existing TRX/token balance able to place market orders — no special privilege is required. An attacker only needs to control (or find) counterpart maker orders whose quantity ratios produce a non-terminating division result relative to the taker's remaining quantity, which is easy to arrange by placing self-crafted maker orders with mismatched, coprime `sellTokenQuantity`/`buyTokenQuantity` pairs.

### Recommendation
Round up (ceiling) the amount debited from the taker (`makerBuyTokenQuantityReceive`) instead of flooring it in `MarketUtils.multiplyAndDivide`, or otherwise ensure the maker is never credited less than the exact/fair proportional amount when the taker receives a maker's entire remaining balance. Any residual dust should be resolved in favor of the resource holder (maker) rather than the consuming party (taker), mirroring the recommended fix of rounding up debt/liability-affecting divisions.

### Proof of Concept
1. Attacker creates a maker sell order for token A with `sellTokenQuantity = 3`, `buyTokenQuantity = 1` (or any ratio producing an inexact division against likely taker amounts), leaving `makerSellRemainQuantity` such that `makerSellRemainQuantity * makerBuyQuantity / makerSellQuantity` has a nonzero remainder (e.g., `makerSellRemainQuantity = 2`).
2. A taker (attacker-controlled second account, or the same account through a proxy) submits a `MarketSellAssetContract` requesting more than the maker's remaining quantity ("taker > maker" branch in `matchSingleOrder`).
3. `makerBuyTokenQuantityReceive = MarketUtils.multiplyAndDivide(2, 1, 3, ...) = floor(2/3) = 0` is credited to the maker (or a similarly small floored value for larger inputs), while the taker receives the maker's full `makerSellRemainQuantity = 2` of token A.
4. Repeating this pattern across numerous crafted maker orders lets the taker accumulate tokens while paying less than the computed fair price each time, extracting value that is never fully debited from the taker's balance — directly analogous to the reported repeated "loan amount = 1" exploit that grew balance while debt (liability) stayed flat. [2](#0-1) [1](#0-0)

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
