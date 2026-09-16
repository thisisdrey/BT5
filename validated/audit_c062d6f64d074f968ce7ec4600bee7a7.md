## Title
Market order matching rounds maker fill quantities down instead of up, causing makers to receive less than the price they committed to - (File: `chainbase/src/main/java/org/tron/core/capsule/utils/MarketUtils.java`)

### Summary
TRON's on-chain order-book market (`MarketSellAssetActuator` / `MarketUtils`) computes the amount of tokens a resting order (the "maker") is owed when matched against an incoming order (the "taker") using `MarketUtils.multiplyAndDivide`, which always truncates (floor-divides) the result. This is the same rounding-direction defect as Bond Protocol's M-5: a price/quantity ratio that must be rounded in favor of the party whose commitment defines the price (the maker) is instead rounded down, systematically shortchanging that party.

### Finding Description
`MarketUtils.multiplyAndDivide` performs `a*b` then `floorDiv(tmp, c)` (or, on overflow fallback, `BigInteger` truncating division), i.e. it always rounds toward zero/negative infinity: [1](#0-0) 

In `MarketSellAssetActuator.matchSingleOrder`, this helper is used to compute `makerBuyTokenQuantityReceive` — the amount of the maker's requested (buy) token that the maker receives in exchange for the sell tokens consumed from their resting order, derived from the maker's own committed price ratio (`makerBuyQuantity / makerSellQuantity`): [2](#0-1) [3](#0-2) 

The maker's order (`sellTokenQuantity` : `buyTokenQuantity`) defines the exact exchange rate the maker is willing to accept, exactly analogous to the Bond whitepaper's price definition. When a partial fill occurs, `makerBuyTokenQuantityReceive` should be rounded up to guarantee the maker never receives less value than their stated price implies for the sell-token amount consumed. Because `multiplyAndDivide` always floors, the maker is paid strictly `<=` the mathematically exact proportional amount, and the truncated remainder is not returned to the maker or accounted for anywhere — it is effectively transferred/lost to the taker side of the trade (the taker correspondingly consumes the maker's sell-tokens without paying the fractional excess).

### Impact Explanation
Every partial match against a maker order can silently underpay the maker relative to the price the maker placed, a direct violation of the pricing guarantee that the order book is supposed to enforce. Because `MAX_MATCH_NUM` allows many maker orders to be consumed in a single taker transaction, and this rounding loss recurs on every single match, a taker (or a taker colluding with many small self-placed maker orders, or repeatedly targeting the same market pair) can accumulate the truncation "dust" from many trades, effectively extracting value from makers over time at the protocol level. This constitutes an unauthorized/unintended transfer of value away from account holders participating in the Market feature, corresponding to a Medium severity accounting/rounding defect matching the scale and nature of the referenced Bond Protocol finding.

### Likelihood Explanation
This path is reachable by any account issuing a `MarketSellAssetContract` (`createMarketSellAsset` transaction), which is fully permissionless and requires no special privilege — matching the "order placer" persona explicitly in-scope. The rounding occurs on essentially every partial match (any time `makerSellRemainQuantity * makerBuyQuantity` is not exactly divisible by `makerSellQuantity`), making the condition trivial to trigger, including deliberately, by controlling the ratios used when placing orders.

### Recommendation
Round `makerBuyTokenQuantityReceive` (and any other quantity computed from another party's committed price ratio in the maker's favor) up rather than down — e.g. add a `mulDivUp`-style helper (`ceil((a*b)/c)`) and use it specifically when computing what a maker is owed, while continuing to use floor/truncation only for quantities computed in favor of the initiating (taker) side, so that no party can be paid less than their stated price entitles them to.

### Proof of Concept
1. Attacker A places a maker sell order via `MarketSellAssetContract`: sell 3 units of Token X for 10 units of Token Y (`sellTokenQuantity=3`, `buyTokenQuantity=10`), i.e. price = 10/3 Y per X.
2. Attacker (or accomplice) B places a taker order that gets partially matched such that `makerSellRemainQuantity` consumed from A's order is, e.g., `1` unit of X.
3. `makerBuyTokenQuantityReceive = multiplyAndDivide(1, 10, 3, ...) = floorDiv(10, 3) = 3` (exact value is `3.33`).
4. Maker A receives only `3` Y instead of the fractionally-correct `3.33` Y for the `1` X sold; the truncated `0.33` Y is never credited to A and is effectively kept on the taker's side.
5. Repeating steps 2-4 across many partial fills (via `MAX_MATCH_NUM` matches per transaction or across many transactions) accumulates a persistent value leak from maker to taker, contradicting the price the maker committed to when placing the order. [4](#0-3)

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

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L453-463)
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
```
