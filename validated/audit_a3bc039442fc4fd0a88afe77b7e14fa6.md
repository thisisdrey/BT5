## Title
Rounding-to-zero in market order "exact-match" branch lets makers lose entire sold token amount for zero payment - (File: `actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java`)

### Summary
`MarketSellAssetActuator.matchSingleOrder` computes the counter-payment a resting ("maker") order receives using integer division that can floor to `0` when the remaining sell quantity is small relative to the order's price ratio. Two of the three match branches ("taker > maker" and implicitly "taker < maker") guard against this rounding-to-zero, but the "taker == maker" (exact full match) branch performs the same division without any zero-check, closes the maker's order as fully filled, and transfers `0` tokens to the maker while the taker still receives the maker's entire remaining sell-token balance.

### Finding Description
In `matchSingleOrder`, when the taker's converted buy quantity exactly equals the maker's remaining sell quantity, the code takes the "taker == maker" branch: [1](#0-0) 

`makerBuyTokenQuantityReceive` is computed as `MarketUtils.multiplyAndDivide(makerSellRemainQuantity, makerBuyQuantity, makerSellQuantity, ...)`, i.e. `floor(makerSellRemainQuantity * makerBuyQuantity / makerSellQuantity)`. This can legitimately equal `0` when `makerSellRemainQuantity * makerBuyQuantity < makerSellQuantity` (e.g. a cheap-priced token with a small dust `makerSellRemainQuantity`). Unlike this branch, the "taker > maker" branch that performs the identical division explicitly checks for this condition and reverts the fill instead of proceeding: [2](#0-1) 

In the "taker == maker" branch, no such check exists. The maker's order is immediately marked `INACTIVE` (fully filled) via `MarketUtils.updateOrderState`, and `addTrxOrToken(makerOrderCapsule, makerBuyTokenQuantityReceive)` is invoked with `makerBuyTokenQuantityReceive == 0`, permanently closing the maker's order while crediting them nothing. Meanwhile the taker receives `takerBuyTokenQuantityReceive = makerOrderCapsule.getSellTokenQuantityRemain()` (the maker's full remaining balance) via `addTrxOrToken(takerOrderCapsule, takerBuyTokenQuantityReceive, takerAccountCapsule)`, and the taker's own consumed amount (`takerSellTokenLeft = takerOrderCapsule.getSellTokenQuantityRemain() - makerBuyTokenQuantityReceive`) is reduced by exactly `0`, meaning the taker pays nothing for the tokens received. [3](#0-2) 

An attacker reachable via a single signed `MarketSellAssetContract` transaction (processed by `MarketSellAssetActuator.execute`/`matchOrder`) can:
1. Observe a resting maker order's price ratio (`makerSellTokenQuantity`/`makerBuyTokenQuantity`) and remaining quantity in the on-chain order book.
2. Optionally submit prior partial-fill orders to reduce the maker's `sellTokenQuantityRemain` to a small "dust" value `R` such that `R * makerBuyQuantity < makerSellQuantity`.
3. Submit a final order with a precisely chosen `sellTokenQuantity` so that `floor(takerSellQuantity * makerSellQuantity / makerBuyQuantity) == R` exactly, triggering the "taker == maker" branch.
4. Receive the maker's full remaining token balance for `0` payment, while the maker's order is closed as completed.

### Impact Explanation
This results in a direct, unauthorized transfer of value: the maker's remaining asset balance is fully consumed and their order closed as "matched," yet they receive `0` tokens in return — a concrete, permanent loss of funds for the maker and an unbacked/free gain for the attacker (taker). This meets the bar for theft of funds via a normal, unprivileged transaction (`MarketSellAssetContract`) reachable by any account.

### Likelihood Explanation
The condition requires only integer arithmetic properties of publicly-known order-book state (price ratio and remaining quantity), both readable before submission. An attacker can compute the exact `takerSellQuantity` needed off-chain and/or arrange partial fills to create the necessary dust remainder, making exploitation practical for orders with favorable (large sell/buy quantity ratio) prices, which are common for low-value tokens.

### Recommendation
Add the same zero-quantity guard used in the "taker > maker" branch to the "taker == maker" branch: if `makerBuyTokenQuantityReceive` computed via `MarketUtils.multiplyAndDivide` is `0`, do not close/fill the maker's order — instead return the maker's remaining sell tokens (as done in the "taker > maker" branch's `makerOrderCapsule.setSellTokenQuantityReturn(); returnSellTokenRemain(makerOrderCapsule); return;`) rather than marking it `INACTIVE` and crediting `0`.

### Proof of Concept
1. Victim places a maker sell order via `MarketSellAssetActuator` with `sellTokenQuantity = S`, `buyTokenQuantity = B` where `B` is small relative to `S` (cheap token price), e.g. `S = 10_000_000`, `B = 3`.
2. Through one or more prior taker trades (or an initial partial match), reduce the maker order's `sellTokenQuantityRemain` to a dust value `R` such that `R * B < S` (e.g. `R = 1_000_000` giving `R*B = 3_000_000 < S = 10_000_000`).
3. Attacker submits a new `MarketSellAssetContract` as taker with `sellTokenQuantity` chosen such that `floor(takerSellQuantity * S / B) == R` exactly (solvable by choosing an appropriate integer in the required range, given attacker controls arbitrary order sizes and can iterate over the readable order book).
4. `matchSingleOrder` enters the `takerBuyTokenQuantityRemain == makerOrderCapsule.getSellTokenQuantityRemain()` branch; `makerBuyTokenQuantityReceive = floor(R*B/S) = 0`; maker's order is set `INACTIVE` and removed from the book at [4](#0-3)  while the taker is credited the maker's full `R` remaining tokens at [5](#0-4)  for a payment of `0`.

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

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L485-491)
```java
    // save makerOrderCapsule
    orderStore.put(makerOrderCapsule.getID().toByteArray(), makerOrderCapsule);

    // add token into account
    addTrxOrToken(takerOrderCapsule, takerBuyTokenQuantityReceive, takerAccountCapsule);
    addTrxOrToken(makerOrderCapsule, makerBuyTokenQuantityReceive);

```
