### Title
Order-book depth grown between transaction construction and inclusion causes `MarketSellAssetActuator` to fully revert instead of partially matching - ([File: actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java])

### Summary
`MarketSellAssetActuator.matchOrder()` iterates over resting maker orders and hard-reverts the entire market-sell transaction with `ContractValidateException("Too many matches...")` once `matchOrderCount` exceeds `MAX_MATCH_NUM` (20). Because the number of matching maker orders at the taker's price is spot-market state that can change between the time a user signs/broadcasts the transaction and the time it is actually executed in a block, an order that was safely under the match-count limit at signing time can exceed it by execution time, causing the whole trade to fail and be rolled back — the exact "stale, check-then-revert on volatile external state" pattern described in the Notional finding.

### Finding Description
`matchOrder()` walks the maker order book price-by-price and order-by-order, incrementing `matchOrderCount` for every matched maker order: [1](#0-0) 

Once the count exceeds the static `MAX_MATCH_NUM` (20), a `ContractValidateException` is thrown from deep inside `execute()`: [2](#0-1) 

This happens after `execute()` has already performed state-mutating steps for the taker (fee deduction, `transferBalanceOrToken`, `createAndSaveOrder`) and inside the same call has been mutating maker order objects in the match loop: [3](#0-2) 

The exception is caught and re-thrown as `ContractExeException`: [4](#0-3) 

`RuntimeImpl.execute()` calls `act.validate()` then `act.execute()` without any pre-check on how many maker orders would need to be matched — that count is only discovered mid-execution, dependent on live order-book depth: [5](#0-4) 

The order book (`pairPriceToOrderStore`/`pairToPriceStore`) is shared, mutable state that other users' `MarketSellAssetContract`/order-cancellation transactions can change every block. A taker transaction can be constructed and signed when the order book at the target price level has ≤20 resting orders (e.g., many small orders freshly placed by other market participants at the same/adjacent price), pass client-side simulation/estimation, and then be broadcast. By the time it is actually included in a block, additional small orders placed by other users at the same price level push the match count for this exact taker order above `MAX_MATCH_NUM`, and the transaction reverts entirely — this is directly analogous to the Notional `_rebalanceCurrency` bug, where a require/threshold check that depends on volatile external state at construction time can flip by execution time and abort the whole operation instead of completing the achievable portion.

Unlike the Notional case (single actor calling `checkRebalance`), this path is reachable by **any unprivileged account that places a market order** via `MarketSellAssetContract` — no special permission is required, matching the "order placer" reachable-actor category.

### Impact Explanation
Because the failure surfaces as `ContractExeException`, the containing DB session for `processTransaction` is not merged/committed (it is rolled back when the surrounding `try (ISession tmpSession = ...)` block exits without `merge()`/`commit()`), so no order-book or balance corruption results from this specific path by itself. The concrete impact is therefore limited to:
- The taker's legitimate market order — that could otherwise have safely matched up to `MAX_MATCH_NUM` maker orders and rested the remainder — fails outright and consumes bandwidth/energy, and must be resubmitted, repeatedly if the book keeps churning near that price level.
- This creates a griefing/DoS vector against specific price levels of the market: an adversary with negligible capital can continuously place (and possibly cancel/refill) many small maker orders at a target price to keep the resting-order count for that pair/price oscillating around `MAX_MATCH_NUM`, causing legitimate large takers at that exact price to repeatedly fail (denial of service against a specific market feature/order-matching functionality), analogous to the delayed-rebalance impact in the original finding (repeated reverts degrading an on-chain financial mechanism).
- Given the strict validation rules for this exercise (only concrete unauthorized operations, theft, fund freezing, node crash/halt, chain split, key disclosure, RCE, or an API becoming unusable), this finding does not cross that bar on its own — the rollback semantics of the actuator/revoking session prevent any corrupted or partially-applied state, and there is no fund loss or freezing demonstrated. This should be treated as informational/low unless it can be combined with another primitive (e.g., a case where the mid-loop mutations to `orderStore`/`pairPriceToOrderStore` are not fully rolled back on this exception path, which was not established from the code reviewed).

### Likelihood Explanation
Likely to occur naturally in an active, contested market (many participants placing/cancelling small orders at popular price levels) without any attacker action, and trivially triggerable by an adversary who deliberately keeps a price level's resting-order count fluctuating near `MAX_MATCH_NUM` with low-cost orders funded by `MarketSellAssetContract`'s `sellTokenQuantity`/`buyTokenQuantity` minimums.

### Recommendation
Do not let `matchOrder()` hard-fail the whole transaction when the match count is exceeded mid-execution. Instead, stop matching once `MAX_MATCH_NUM` is reached, keep the already-matched portion, and place the taker's remaining unmatched quantity back into the order book (the same path used when `orderCapsule.getSellTokenQuantityRemain() != 0`), e.g.:
```java
matchOrderCount++;
if (matchOrderCount > MAX_MATCH_NUM) {
  break; // stop matching further, remainder gets saved back to the book
}
```
instead of throwing `ContractValidateException`. This preserves the partial-success semantics the Notional fix recommended (skip/stop rather than revert the whole transaction) while avoiding wasted fees and DoS surface against a specific price level.

### Proof of Concept
1. An attacker (or normal market activity) maintains ~21+ small resting sell orders at price `P` for pair (`A`/`B`) via repeated `MarketSellAssetContract` calls.
2. A legitimate user signs and broadcasts a `MarketSellAssetContract` transaction intending to buy at price `P`, which at signing/simulation time would only need to match ≤20 of these resting orders (e.g., they simulated against a slightly earlier order-book snapshot, or additional orders arrived in the same block window before their transaction is packed).
3. Between broadcast and block inclusion, more orders accumulate at price `P` (from ordinary trading activity or the attacker), pushing the number of orders the taker's transaction must walk through above `MAX_MATCH_NUM`.
4. On execution, `matchOrder()` throws once `matchOrderCount > MAX_MATCH_NUM` at `actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java:358`, `execute()` converts this to `ContractExeException`, and the user's transaction fails entirely, consuming resources and requiring resubmission — repeating this against the same price level constitutes a low-cost griefing/DoS vector against market order execution at that level.

Note: I was unable to fully trace whether `matchOrderCount` accrues purely as an in-memory execution artifact per transaction (and is therefore always safely discarded on rollback) versus whether any store writes performed earlier in the same `matchOrder` loop iteration (e.g., `orderStore.put`, `pairPriceToOrderStore.delete`) persist independently of the later thrown exception; based on the code reviewed, these writes occur through the same `Repository`/`Store` objects used elsewhere in the actuator and should be covered by the same revoking-DB session rollback, but this was not exhaustively confirmed against `SnapshotManager`/session-merge call sites for this specific exception path.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L125-147)
```java
      // fee
      accountCapsule.setBalance(accountCapsule.getBalance() - fee);
      // add to blackhole address
      if (dynamicStore.supportBlackHoleOptimization()) {
        dynamicStore.burnTrx(fee);
      } else {
        adjustBalance(accountStore, accountStore.getBlackhole(), fee);
      }
      // 1. transfer of balance
      transferBalanceOrToken(accountCapsule);

      // 2. create and save order
      MarketOrderCapsule orderCapsule = createAndSaveOrder(accountCapsule, contract);

      // 3. match order
      matchOrder(orderCapsule, takerPrice, ret, accountCapsule);

      // 4. save remain order into order book
      if (orderCapsule.getSellTokenQuantityRemain() != 0) {
        saveRemainOrder(orderCapsule);
      }

      orderStore.put(orderCapsule.getID().toByteArray(), orderCapsule);
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L152-159)
```java
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

**File:** framework/src/main/java/org/tron/common/runtime/RuntimeImpl.java (L52-60)
```java
    if (actuator2 != null) {
      actuator2.validate(context);
      actuator2.execute(context);
    } else {
      for (Actuator act : actuatorList) {
        act.validate();
        act.execute(context.getProgramResult().getRet());
      }
    }
```
