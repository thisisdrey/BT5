### Title
Uncaught `ArithmeticException` in `MarketSellAssetActuator.matchOrder`/`matchSingleOrder` loop can abort order matching mid-way and desynchronize order-book/account state - ([File: actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java])

### Summary
`MarketSellAssetActuator.execute` iterates over a list of maker orders inside `matchOrder`, calling `matchSingleOrder` for each match, similar to the Gitcoin `MerklePayoutStrategyImplementation.payout` pattern where a single failing iteration (`_transferAmount`) aborts the whole batch. In `matchSingleOrder`, quantity calculations use `subtractExact`/`addExact`/`MarketUtils.multiplyAndDivide`, which throw `ArithmeticException` on overflow. `execute()` only catches `ItemNotFoundException | InvalidProtocolBufferException | BalanceInsufficientException | ContractValidateException`, so an `ArithmeticException` from a single unlucky maker match is **not caught** and escapes the actuator. [1](#0-0) 

### Finding Description
The matching loop in `matchOrder` walks the resting maker order book price level by price level, calling `matchSingleOrder` for each maker order at the best price: [2](#0-1) 

`matchSingleOrder` performs exact arithmetic (`subtractExact`, `addExact`, `MarketUtils.multiplyAndDivide`) whose result depends on quantities set by makers, which the taker (the transaction sender) cannot control or foresee: [3](#0-2) 

`execute()`'s catch clause explicitly enumerates checked/expected exception types, omitting `ArithmeticException` and any other `RuntimeException`: [4](#0-3) 

This mirrors the Gitcoin bug class: a loop processing multiple independent "distributions" (here, maker order matches) where one item's failure is not isolated and can propagate an uncaught exception out of the whole batch operation. Unlike the Gitcoin case (which merely reverts the enclosing transaction — the normal/expected behavior for actuators), here the escaping exception is a *generic* `RuntimeException` rather than the actuator's own `ContractExeException`. If this uncaught exception is not defensively handled by the block-application caller in `Manager` (only `ContractExeException` and similar checked types are typically expected there), it can propagate out of `processTransaction`/block application, aborting/crashing that code path for every node applying the block — a much more severe outcome than the intended "transaction fails" semantics.

### Impact Explanation
If the exception is not caught somewhere generic in the block-application path, this becomes a node crash / chain halt vector reachable by any account broadcasting a `MarketSellAssetContract` transaction, since the actuator has already mutated `orderStore`/`pairPriceToOrderStore` state before the exception is thrown (partial state mutation followed by an uncaught exception can also corrupt the order book if the surrounding transaction-processing does not roll back non-account state consistently). At minimum, this represents unhandled-exception-based denial-of-service against the actuator/transaction-processing pipeline, matching the "node crash or halt" acceptance criterion.

### Likelihood Explanation
Reachability requires only a single signed `MarketSellAssetContract` transaction (a public, unprivileged wallet operation) matching against an existing resting order whose quantities are crafted (or organically occur) to produce a `Math.subtractExact`/`addExact`/`multiplyAndDivide` overflow. An attacker can place maker orders with extreme quantities/prices to try to trigger this overflow deliberately when their own or a victim's sell order matches. This is somewhat probabilistic/crafted (requires overflow-triggering quantities), which is a moderate rather than trivial precondition, but it is within reach of an ordinary market participant without needing SR/witness/committee privileges.

### Recommendation
- Broaden the `catch` clause in `MarketSellAssetActuator.execute` to also catch `ArithmeticException` (and ideally a generic `RuntimeException` fallback), converting it into a `ContractExeException` with `ret.setStatus(fee, code.FAILED)`, consistent with how other exact-math actuators (e.g., `ExchangeInjectActuator`, `ExchangeWithdrawActuator`) already catch `ArithmeticException`.
- Ensure any state already mutated on `orderStore`/`pairPriceToOrderStore` inside `matchOrder`/`matchSingleOrder` is not persisted unless the whole actuator completes successfully, to avoid partial/inconsistent order-book states if an exception is thrown partway through the loop.
- Add bounds validation before performing the exact-math order matching to reject orders that could realistically overflow long arithmetic, rather than relying on runtime exceptions.

### Proof of Concept
Not independently reproducible from static analysis alone: the exact quantity/price combination required to trigger `subtractExact`/`addExact`/`multiplyAndDivide` overflow inside `matchSingleOrder` depends on existing order-book state and would need to be validated empirically (e.g., via `MarketSellAssetActuatorTest`-style unit test crafting a maker order with quantities near `Long.MAX_VALUE` matched against a taker order) to confirm the exception is truly uncaught by any wrapping logic in `Manager`/block application. This should be verified with a live/test build before treating it as confirmed-exploitable.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L150-159)
```java
      ret.setOrderId(orderCapsule.getID());
      ret.setStatus(fee, code.SUCESS);
    } catch (ItemNotFoundException
        | InvalidProtocolBufferException
        | BalanceInsufficientException
        | ContractValidateException e) {
      logger.debug(e.getMessage(), e);
      ret.setStatus(fee, code.FAILED);
      throw new ContractExeException(e.getMessage());
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L342-360)
```java
      while (takerCapsule.getSellTokenQuantityRemain() != 0
          && !orderIdListCapsule.isOrderEmpty()) {
        byte[] orderId = orderIdListCapsule.getHead();
        MarketOrderCapsule makerOrderCapsule = orderStore.get(orderId);

        matchSingleOrder(takerCapsule, makerOrderCapsule, ret, takerAccountCapsule);

        // remove order
        if (makerOrderCapsule.getSellTokenQuantityRemain() == 0) {
          // remove from market order list
          orderIdListCapsule.removeOrder(makerOrderCapsule, orderStore,
              pairPriceKey, pairPriceToOrderStore);
        }

        matchOrderCount++;
        if (matchOrderCount > MAX_MATCH_NUM) {
          throw new ContractValidateException("Too many matches. MAX_MATCH_NUM = " + MAX_MATCH_NUM);
        }
      }
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L402-453)
```java
    long takerBuyTokenQuantityRemain = MarketUtils
        .multiplyAndDivide(takerSellRemainQuantity, makerSellQuantity, makerBuyQuantity,
            this.disableJavaLangMath());

    if (takerBuyTokenQuantityRemain == 0) {
      // quantity too small, return sellToken to user
      takerOrderCapsule.setSellTokenQuantityReturn();
      MarketUtils.returnSellTokenRemain(takerOrderCapsule, takerAccountCapsule,
          dynamicStore, assetIssueStore);
      MarketUtils.updateOrderState(takerOrderCapsule, State.INACTIVE, marketAccountStore);
      return;
    }

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
    } else {
```
