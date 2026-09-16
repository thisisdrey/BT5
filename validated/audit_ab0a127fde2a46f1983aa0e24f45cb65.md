### Title
Missing minimum order size / order-count cap in TRC10 Market matching allows griefing DoS of `MarketSellAssetActuator` via forced "Too many matches" reverts - (File: `actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java`)

### Summary
`MarketSellAssetActuator` enforces only a positive-quantity check (`sellTokenQuantity <= 0 || buyTokenQuantity <= 0`) and a per-account maximum *active* order count (`MAX_ACTIVE_ORDER_NUM = 100`), but imposes **no minimum order size**. Any unprivileged account (an "order placer") can therefore flood a trading-pair's order book with many tiny/dust maker orders at the best price. When a legitimate taker later submits a normal-sized `MarketSellAssetContract` against that pair, `matchOrder()` walks the maker order list and increments `matchOrderCount`; once it exceeds the fixed `MAX_MATCH_NUM = 20`, the actuator throws `ContractValidateException("Too many matches. MAX_MATCH_NUM = " + MAX_MATCH_NUM)`, causing the taker's transaction to fail. This is a direct on-chain analog of the reported "lack of minimum size" griefing pattern — an attacker fragments liquidity into countless tiny orders to break normal order matching for everyone else.

### Finding Description
In `MarketSellAssetActuator.validate()`, the only size-related checks are: [1](#0-0) 

and the per-account order-count cap: [2](#0-1) 

There is no minimum `sellTokenQuantity`/`buyTokenQuantity`, so an attacker can create orders as small as `1` unit each, up to 100 active orders per account, and can repeat this from many distinct accounts (Sybil), for the same trading pair.

During matching, `matchOrder()` iterates maker orders at the best price and hard-fails once more than `MAX_MATCH_NUM` (20) maker orders are consumed in a single taker transaction: [3](#0-2) 

This exception is thrown from inside `execute()`, which is caught by the generic `ContractValidateException` handler that marks the transaction FAILED and rethrows `ContractExeException`: [4](#0-3) 

Because a swarm of dust maker orders can occupy the top 20+ price-matching slots for a pair, any legitimate taker order that would otherwise match cleanly is forced through the same `matchOrder` loop and hits the `MAX_MATCH_NUM` limit, reverting the taker's transaction (with fee/bandwidth already consumed and without a successful trade).

### Impact Explanation
This lets any account grief the on-chain TRC10 exchange/market feature for a specific token pair: legitimate takers cannot get their `MarketSellAssetContract` transactions to succeed because a small number of "aggregator"-controlled accounts can permanently keep 20+ dust orders resting at (or ahead of) the best price. Victims lose transaction fees/bandwidth on failed transactions and are unable to reliably trade the pair, which is a concrete availability/griefing impact on the exchange order-matching path reachable by any unprivileged order placer — matching the analog category "exchange and market order handling."

### Likelihood Explanation
Likelihood is high: creating a `MarketSellAssetContract` with `sellTokenQuantity = 1` costs only the market sell fee (`getMarketSellFee()`) and requires no special privilege — this is fully reachable by any TRC10 holder. Reaching the 100-order-per-account cap is trivial, and the attack can be repeated across multiple accounts to permanently occupy the matching window for any actively traded pair.

### Recommendation
Introduce a minimum order size (absolute or relative to `getMarketQuantityLimit()`/asset precision) enforced in `MarketSellAssetActuator.validate()` alongside the existing `sellTokenQuantity <= 0` check, and/or make the per-transaction match limit (`MAX_MATCH_NUM`) dynamically skip-and-continue past dust orders instead of hard-failing the whole taker transaction, so that a handful of tiny maker orders cannot block matching for legitimate, appropriately sized orders.

### Proof of Concept
1. Attacker (or several Sybil accounts) issue up to 100 `MarketSellAssetContract` orders each for pair (A, B), each with `sellTokenQuantity = 1`, `buyTokenQuantity = 1`, all landing at/near the best price for that pair — allowed because `validate()` only checks `> 0` and the per-account cap of 100.
2. A legitimate user submits a normal-sized `MarketSellAssetContract` on the same pair.
3. `matchOrder()` consumes the dust maker orders one-by-one; once `matchOrderCount` exceeds `MAX_MATCH_NUM = 20` (see `actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java:356-359`), the actuator throws `ContractValidateException("Too many matches. MAX_MATCH_NUM = 20")`, and the legitimate user's transaction fails, paying fees without completing the trade.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L139-159)
```java
      // 3. match order
      matchOrder(orderCapsule, takerPrice, ret, accountCapsule);

      // 4. save remain order into order book
      if (orderCapsule.getSellTokenQuantityRemain() != 0) {
        saveRemainOrder(orderCapsule);
      }

      orderStore.put(orderCapsule.getID().toByteArray(), orderCapsule);
      accountStore.put(accountCapsule.createDbKey(), accountCapsule);

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

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L232-239)
```java
    // check order num
    MarketAccountOrderCapsule marketAccountOrderCapsule = marketAccountStore
        .getUnchecked(ownerAddress);
    if (marketAccountOrderCapsule != null
        && marketAccountOrderCapsule.getCount() >= MAX_ACTIVE_ORDER_NUM) {
      throw new ContractValidateException(
          "Maximum number of orders exceeded，" + MAX_ACTIVE_ORDER_NUM);
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
