Based on my research, I found a valid analog to the reported "rounding error favors the buyer" bug class in java-tron's on-chain order-matching engine (`MarketSellAssetActuator`), which is reachable by any account submitting a `MarketSellAssetContract` transaction.

### Title
Rounding-down in `matchSingleOrder`'s exact-match branch lets a taker underpay a maker, permanently leaking maker funds - (File: actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java)

### Summary
`MarketSellAssetActuator.matchSingleOrder` computes cross amounts for order matching using `MarketUtils.multiplyAndDivide`, which always performs `floorDiv` (round-towards-zero for positive operands) [1](#0-0) . In the "taker == maker" branch of `matchSingleOrder`, the taker's received amount (`takerBuyTokenQuantityReceive`) is set to the maker's *exact* remaining sell quantity, while the amount the maker receives in return (`makerBuyTokenQuantityReceive`) is computed via this same floor-rounding division — but, unlike the sibling "taker > maker" branch, there is no `== 0` (or any) sanity check guarding this value [2](#0-1) .

### Finding Description
In `matchSingleOrder`:
- `takerBuyTokenQuantityRemain` is computed as `floor(takerSellRemainQuantity * makerSellQuantity / makerBuyQuantity)` [3](#0-2) .
- When `takerBuyTokenQuantityRemain == makerOrderCapsule.getSellTokenQuantityRemain()` (the "taker == maker" exact-fill branch), the code sets `takerBuyTokenQuantityReceive = makerOrderCapsule.getSellTokenQuantityRemain()` (the maker's full remaining sell balance, no rounding), but computes what the maker gets back with the *reciprocal* ratio: `makerBuyTokenQuantityReceive = floor(makerSellRemainQuantity * makerBuyQuantity / makerSellQuantity)` [4](#0-3) .

Because `multiplyAndDivide` always truncates towards zero (rounds down) [1](#0-0) , the amount actually taken from the taker and credited to the maker (`makerBuyTokenQuantityReceive`) can be strictly less than the value implied by the maker's posted price for the token quantity the taker receives in full. This is the exact rounding-direction error described in the report: the quantity flowing *into* the counterparty (maker) is rounded down instead of up, silently favoring the party receiving output tokens (the taker) at the counterparty's expense.

Critically, the sibling "taker > maker" branch explicitly guards against the zero-rounding edge case (`if (makerBuyTokenQuantityReceive == 0) { ... return; }`) [5](#0-4) , but the "taker == maker" branch performs the identical rounding computation with **no such guard**, so a fully or near-fully rounded-away payment silently proceeds and the maker's order is closed out as fully filled.

### Impact Explanation
Any user can place TRC10 sell orders via `MarketSellAssetContract` with skewed sell/buy ratios and remaining quantities engineered to land in the "taker == maker" branch, causing `makerBuyTokenQuantityReceive` to be rounded down relative to `takerBuyTokenQuantityReceive`. This produces an unbacked value transfer: the maker's asset store is debited the full `makerSellRemainQuantity`, credited only the rounded-down `makerBuyTokenQuantityReceive`, while the taker receives the full token amount for less than the market-priced cost. Repeated over many self-matched or adversary-matched orders, this is a permanent theft/loss-of-funds vector for maker liquidity, matching the "medium" severity of the original finding (fee/rate degradation, and in extreme ratios, near-free tokens since there's no zero-value guard in this branch).

### Likelihood Explanation
The path is reachable by any unprivileged account through the standard `MarketSellAssetContract` order-placement and order-matching flow — no special privileges are required, and a user fully controls both the maker order (their own posted price/quantity) and the taker order (or matches against any similarly-shaped existing maker order), making the precise integer ratios needed to trigger and repeat the rounding loss straightforward to engineer.

### Recommendation
Round in favor of the protocol/maker for amounts flowing into a party as payment: use `Math.ceilDiv`-style (round-up) arithmetic for `makerBuyTokenQuantityReceive` in the "taker == maker" branch (and audit the analogous "taker > maker" branch, which currently only null-guards a rounded-down value rather than rounding up), and add an explicit minimum-received check consistent with the guard already present elsewhere in the function.

### Proof of Concept
1. Attacker (or two colluding accounts) posts a maker sell order via `MarketSellAssetContract` with `sellTokenQuantity`/`buyTokenQuantity` chosen so that `buyTokenQuantity / sellTokenQuantity` is a non-terminating ratio (e.g., sell 3 unit A for 1 unit TRX), and lets it partially fill down to a `sellTokenQuantityRemain` value where `floor(sellTokenQuantityRemain * buyTokenQuantity / sellTokenQuantity)` truncates away a meaningful fraction of a unit.
2. A taker order is placed/matched such that `takerBuyTokenQuantityRemain` (computed via `multiplyAndDivide`) exactly equals that `sellTokenQuantityRemain`, entering the "taker == maker" branch in `matchSingleOrder` [2](#0-1) .
3. `matchSingleOrder` executes: taker receives `makerOrderCapsule.getSellTokenQuantityRemain()` (full amount) via `addTrxOrToken`, while maker only receives the floor-rounded `makerBuyTokenQuantityReceive`, which can be strictly less than the fair-priced amount — verifiable by comparing `makerSellRemainQuantity * makerBuyQuantity` (exact) against `makerBuyTokenQuantityReceive * makerSellQuantity` before and after the match.

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
