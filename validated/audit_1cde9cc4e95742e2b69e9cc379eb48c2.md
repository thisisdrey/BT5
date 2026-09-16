Based on my analysis, I found a valid analog in java-tron's on-chain order book (`Market*` actuators), which is the closest structural equivalent to 1inch's order matching/invalidation logic.

### Title
Maker order permanently stuck with stale ACTIVE/remain state after zero-receive rounding branch skips `orderStore.put` — ([File: actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java])

### Summary
`MarketSellAssetActuator.matchSingleOrder` closes a maker's order out of the active order book (removes it from `MarketAccountStore` and the price-linked list) whenever, due to integer-division rounding, the computed payout to the maker (`makerBuyTokenQuantityReceive`) is `0`, refunding the maker's remaining sell tokens. However, in that specific code path the method `return`s before the maker's `MarketOrderCapsule` is ever persisted via `orderStore.put(...)`, unlike every other branch of the function.

### Finding Description
In the "taker > maker" branch of `matchSingleOrder`: [1](#0-0) 

`MarketUtils.updateOrderState(makerOrderCapsule, State.INACTIVE, marketAccountStore)` is called unconditionally at line 465, before checking whether `makerBuyTokenQuantityReceive == 0`. This mutates the in-memory `orderCapsule` object's state and removes the order from the maker's active order list in `MarketAccountStore` (persisted immediately) [2](#0-1) .

When the rounding produces `makerBuyTokenQuantityReceive == 0`, the code calls `returnSellTokenRemain(makerOrderCapsule)` (which zeroes `sellTokenQuantityRemain` in the in-memory object and refunds it to the maker's account, persisted to `accountStore`), then immediately `return`s [3](#0-2) . This skips the line that persists the maker's updated `MarketOrderCapsule` (`orderStore.put(makerOrderCapsule.getID().toByteArray(), makerOrderCapsule)`), reached only at the end of the method for all other branches [4](#0-3) .

The caller `matchOrder()` only removes the order from the price-indexed match structure (`pairPriceToOrderStore`) by checking the in-memory object's `getSellTokenQuantityRemain() == 0` — it does not re-persist `orderStore` either [5](#0-4) . As a result, the on-disk `MarketOrderStore` record for that order retains its **stale pre-match `State` (e.g. `ACTIVE`) and stale, non-zero `sellTokenQuantityRemain`**, even though the maker already had that remaining balance refunded to their account and the order was removed from both the account's active order list and the matching engine's order book.

### Impact Explanation
Because `MarketCancelOrderActuator.validate()` checks liveness via `marketOrderCapsule.isActive()`, which reads the (stale) persisted `State` field directly from `orderStore` [6](#0-5) , the maker (owner of the order) could subsequently invoke `MarketCancelOrderActuator` on this "phantom" order. Because the stale record still reports `isActive() == true`, validation passes, and `execute()` calls `MarketUtils.returnSellTokenRemain(orderCapsule, ...)` again using the stale (large, non-zero) `sellTokenQuantityRemain` value [7](#0-6) , crediting the maker's account a second time with tokens that were already refunded once — an unbacked-balance / double-mint condition for TRX or TRC10 assets, minus the market cancel fee. (Whether this second credit is ultimately persisted depends on whether the subsequent `pairPriceToOrderStore.get(pairPriceKey)` lookup at line 118 throws `ItemNotFoundException` — since that key was already deleted during the earlier match — and on whether the enclosing transaction-processing revoking session rolls back all `accountStore`/`orderStore` writes made earlier in the same `execute()` call when the actuator later throws. I was not able to fully verify within the available time whether java-tron's per-transaction session (`SnapshotManager`/`TronStoreWithRevoking`) reverts intra-`execute()` writes on a thrown `ContractExeException`, so the double-refund's ultimate persistence is **unconfirmed** and should be validated with a live/unit test before treating this as a fully proven unbacked-balance bug.)

Independent of the double-refund question, the confirmed, provable bug is a **data-consistency corruption of `MarketOrderStore`**: an attacker (any taker) can force any maker order into a state where the on-chain order record permanently disagrees with the account/order-book bookkeeping (stale `ACTIVE` + stale non-zero remain, while tokens were already returned and the order removed from matching structures). This can be triggered by any account submitting a `MarketSellAssetContract` transaction that matches against an existing order once its remainder is reduced enough to trigger a rounding-to-zero payout — directly analogous to the 1inch RFQ issue where a taker can force invalidation of a maker's remaining order for a disproportionately small cost, undermining the reliability of the order book for makers.

### Likelihood Explanation
Reaching the `makerBuyTokenQuantityReceive == 0` branch requires `makerSellRemainQuantity * makerBuyQuantity < makerSellQuantity` (floor-division rounds to 0), which cannot occur on a maker's very first match (since at that point `makerSellRemainQuantity == makerSellQuantity`, making the ratio always `>= 1` because `buyQuantity > 0`). It requires the maker's order to already have a small remaining balance relative to its original sell/buy ratio — achievable by an attacker who first partially fills the order down to a favorable remainder (paying full price for those fills) and then submits one more precisely-sized taker order to trigger the zero-payout branch. This is a deterministic, attacker-triggerable code path reachable via ordinary `MarketSellAssetContract` transactions, requiring no special privileges, only gas/fee and price calculation.

### Recommendation
- In `MarketSellAssetActuator.matchSingleOrder`, ensure `orderStore.put(makerOrderCapsule.getID().toByteArray(), makerOrderCapsule)` is executed on **every** code path that mutates `makerOrderCapsule`, including the `makerBuyTokenQuantityReceive == 0` early-return branch (e.g., persist immediately after `returnSellTokenRemain(makerOrderCapsule)` and before `return`).
- Refactor `matchSingleOrder` to persist maker/taker order state consistently, ideally via a single exit point, rather than persisting only in the "normal" path and relying on early `return` to skip it.
- Add a guard so `MarketCancelOrderActuator` cannot act on an order whose `sellTokenQuantityRemain` is already `0`, independent of the (possibly stale) `State` field, as defense in depth.
- Add regression tests specifically covering the zero-receive rounding branch (`makerSellQuantity > makerBuyQuantity` with a small `makerSellRemainQuantity`) verifying `orderStore` reflects `State.INACTIVE` and `sellTokenQuantityRemain == 0` after the match, and verifying `MarketCancelOrderActuator` correctly rejects a subsequent cancel attempt on that order.

### Proof of Concept
1. Maker submits a `MarketSellAssetContract` order selling token A for token B with `sellTokenQuantity` set much larger than `buyTokenQuantity` (e.g., sell 1,000,000 A for 1 B) — establishing a ratio where `buyQuantity/sellQuantity` is far below 1.
2. Attacker submits one or more `MarketSellAssetContract` taker orders that partially fill the maker's order (via the "taker < maker" branch) until `makerOrderCapsule.getSellTokenQuantityRemain()` is reduced to a value `R` such that `floor(R * makerBuyQuantity / makerSellQuantity) == 0` (e.g., `R` below `makerSellQuantity/makerBuyQuantity`).
3. Attacker submits a final taker order sized so that `takerBuyTokenQuantityRemain > R` (entering the "taker > maker" branch in `matchSingleOrder`).
4. This triggers the `makerBuyTokenQuantityReceive == 0` branch: the maker's order is marked `INACTIVE` in memory and removed from `MarketAccountStore`/`pairPriceToOrderStore`, the maker is refunded `R` tokens, but `orderStore.get(makerOrderId)` still reports stale `State.ACTIVE` and `sellTokenQuantityRemain == R` (never overwritten).
5. Query `GetMarketOrderById` (gRPC/HTTP `wallet/getmarketorderbyid`) for the maker's order id to observe the stale/incorrect persisted state — confirming the on-chain record is corrupted relative to actual balances and order-book membership.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L349-354)
```java
        // remove order
        if (makerOrderCapsule.getSellTokenQuantityRemain() == 0) {
          // remove from market order list
          orderIdListCapsule.removeOrder(makerOrderCapsule, orderStore,
              pairPriceKey, pairPriceToOrderStore);
        }
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

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L485-486)
```java
    // save makerOrderCapsule
    orderStore.put(makerOrderCapsule.getID().toByteArray(), makerOrderCapsule);
```

**File:** chainbase/src/main/java/org/tron/core/capsule/utils/MarketUtils.java (L251-262)
```java
  public static void updateOrderState(MarketOrderCapsule orderCapsule,
      State state, MarketAccountStore marketAccountStore) throws ItemNotFoundException {
    orderCapsule.setState(state);

    // remove from account order list
    if (state == State.INACTIVE || state == State.CANCELED) {
      MarketAccountOrderCapsule accountOrderCapsule = marketAccountStore
          .get(orderCapsule.getOwnerAddress().toByteArray());
      accountOrderCapsule.removeOrder(orderCapsule.getID());
      marketAccountStore.put(accountOrderCapsule.createDbKey(), accountOrderCapsule);
    }
  }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/MarketOrderCapsule.java (L162-164)
```java
  public boolean isActive(){
    return this.order.getState() == State.ACTIVE;
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketCancelOrderActuator.java (L96-108)
```java
      // fee
      accountCapsule.setBalance(accountCapsule.getBalance() - fee);
      if (dynamicStore.supportBlackHoleOptimization()) {
        dynamicStore.burnTrx(fee);
      } else {
        adjustBalance(accountStore, accountStore.getBlackhole(), fee);
      }
      // 1. return balance and token
      MarketUtils
          .returnSellTokenRemain(orderCapsule, accountCapsule, dynamicStore, assetIssueStore);

      MarketUtils.updateOrderState(orderCapsule, State.CANCELED, marketAccountStore);
      accountStore.put(orderCapsule.getOwnerAddress().toByteArray(), accountCapsule);
```
