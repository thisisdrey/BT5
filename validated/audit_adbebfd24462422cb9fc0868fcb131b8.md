I found a strong analog in the on-chain TRC10 order-matching logic (`MarketSellAssetActuator`), which shares the same bug class as the report: a floor-division ratio calculation can round down to `0` while a state change representing a real asset transfer still executes with a non-zero amount, letting one party keep an asset without paying the counter-value. [1](#0-0) 

### Title
TRC10 exchange order matching can round the maker's proceeds to zero, letting the taker take the maker's tokens for free - (File: `actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java`)

### Summary
In `MarketSellAssetActuator.matchSingleOrder`, when a taker order exactly consumes the remaining quantity of a maker order (`takerBuyTokenQuantityRemain == makerOrderCapsule.getSellTokenQuantityRemain()`), the amount owed back to the maker is computed with an unguarded floor-division:
```
makerBuyTokenQuantityReceive = MarketUtils
    .multiplyAndDivide(makerSellRemainQuantity, makerBuyQuantity, makerSellQuantity, this.disableJavaLangMath());
takerBuyTokenQuantityReceive = makerOrderCapsule.getSellTokenQuantityRemain();
...
makerOrderCapsule.setSellTokenQuantityRemain(0);
``` [2](#0-1) 

Unlike the sibling "taker > maker" branch a few lines below, which explicitly checks `if (makerBuyTokenQuantityReceive == 0)` and returns the unsold remainder to the maker instead of letting the order vanish for free, [3](#0-2)  the "taker == maker" branch performs no such check. It unconditionally sets the maker's `sellTokenQuantityRemain` to `0` (i.e., consumes and transfers away all of the maker's remaining sell-side tokens to the taker via `takerBuyTokenQuantityReceive = makerOrderCapsule.getSellTokenQuantityRemain()`) while crediting the maker only `makerBuyTokenQuantityReceive`, which — because `MarketUtils.multiplyAndDivide` performs `floorDiv(a*b, c)` — can legitimately be `0` for small `makerSellRemainQuantity`.

`MarketUtils.multiplyAndDivide` is the floor-rounding helper responsible for the truncation: [4](#0-3) 

The credit to the taker/maker accounts is finally applied via `addTrxOrToken`, which simply adds whatever quantity (including `0`) was computed, without any minimum-fill guard: [5](#0-4) 

### Finding Description
The root cause mirrors the reported class of bug exactly: a rounding-down integer conversion (`floorDiv`) is used to compute the amount owed for a real, non-zero balance/asset movement, and the code does not verify that the computed amount is non-zero before committing the state change. In `_postTransferPrimeCashUpdate`, `netUnderlyingChange` can round to `0` while the actual external transfer is non-zero. Here, `makerBuyTokenQuantityReceive` can round to `0` while `makerOrderCapsule`'s entire remaining sell-side balance (`getSellTokenQuantityRemain()`) is unconditionally transferred to the taker and the maker's order is marked fully filled (`setSellTokenQuantityRemain(0)`).

The code author was clearly aware of this exact rounding hazard — the very same `multiplyAndDivide` computation is protected by a `== 0` guard three branches later in `matchSingleOrder`, with a comment explaining why it is believed not to happen in that specific branch — but the equivalent guard is missing from the "taker == maker" branch.

### Impact Explanation
This lets a taker place an order engineered to exactly match a maker's tiny remaining order quantity such that the reciprocal exchange-rate computation floors to zero. The maker's remaining TRC10 token balance is transferred to the taker's account for free (or for a token amount far below the agreed price), while the maker's order is closed as fully executed with `0` proceeds credited. This is a concrete unauthorized/unbacked transfer of value from the maker to the taker, i.e., theft of funds through the exchange order book — no privileged role required.

### Likelihood Explanation
Any account can place both the maker order (with unfavorable-but-tiny remaining quantity/price ratio) and the exact taker order needed to trigger the "taker == maker" quantity match with a floor-to-zero maker proceeds calculation. Both `MarketSellAssetContract` calls are ordinary, unprivileged, broadcastable transactions, so exploitation only requires two self-controlled accounts and careful selection of `sellTokenQuantity`/`buyTokenQuantity` ratios — well within reach of any transaction broadcaster.

### Recommendation
In the "taker == maker" branch of `matchSingleOrder`, add the same zero-check used in the "taker > maker" branch: if `makerBuyTokenQuantityReceive == 0`, return the maker's remaining unsold tokens via `MarketUtils.returnSellTokenRemain` (or otherwise reject/defer the match) rather than transferring the taker the full remaining maker balance while crediting the maker `0`.

### Proof of Concept
1. Maker creates a sell order with `sellTokenQuantity = S`, `buyTokenQuantity = B` such that the price ratio `B/S` is very small (e.g., `S = 1000`, `B = 1`).
2. Maker's order partially fills over time (or is created directly) until `makerSellRemainQuantity` (call it `r`) is small enough that `floorDiv(r * B, S) == 0` (e.g., `r = 1`, `B = 1`, `S = 1000` → `floorDiv(1*1, 1000) = 0`).
3. Taker submits a `MarketSellAssetContract` whose computed `takerBuyTokenQuantityRemain` (via `MarketUtils.multiplyAndDivide`) exactly equals `r`, landing in the `takerBuyTokenQuantityRemain == makerOrderCapsule.getSellTokenQuantityRemain()` branch of `matchSingleOrder`.
4. `matchSingleOrder` executes: `takerBuyTokenQuantityReceive = r` (transferred in full to taker via `addTrxOrToken`), `makerBuyTokenQuantityReceive = 0` (credited to maker), `makerOrderCapsule.setSellTokenQuantityRemain(0)`.
5. Result: the taker receives `r` tokens from the maker's order while the maker receives nothing, and the maker's order is closed as fully executed.

### Citations

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

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L537-562)
```java
  // for taker
  private void addTrxOrToken(MarketOrderCapsule orderCapsule, long num,
      AccountCapsule accountCapsule) {

    byte[] buyTokenId = orderCapsule.getBuyTokenId();
    if (Arrays.equals(buyTokenId, "_".getBytes())) {
      accountCapsule.setBalance(addExact(accountCapsule.getBalance(), num));
    } else {
      accountCapsule
          .addAssetAmountV2(buyTokenId, num, dynamicStore, assetIssueStore);
    }
  }

  private void addTrxOrToken(MarketOrderCapsule orderCapsule, long num) {
    AccountCapsule accountCapsule = accountStore
        .get(orderCapsule.getOwnerAddress().toByteArray());

    byte[] buyTokenId = orderCapsule.getBuyTokenId();
    if (Arrays.equals(buyTokenId, "_".getBytes())) {
      accountCapsule.setBalance(addExact(accountCapsule.getBalance(), num));
    } else {
      accountCapsule
          .addAssetAmountV2(buyTokenId, num, dynamicStore, assetIssueStore);
    }
    accountStore.put(orderCapsule.getOwnerAddress().toByteArray(), accountCapsule);
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
