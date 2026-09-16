### Title
Unbounded revert propagation in `MarketSellAssetActuator.matchOrder` can permanently trap taker/maker funds in the order book - (File: `actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java`)

### Summary
The external report describes a "parent/child" pattern where a top-level, otherwise-successful operation (`onReward`) is forced to revert because a sub-operation (a child rewarder) throws, blocking the user's legitimate `withdraw()`. The reachable java-tron analog is `MarketSellAssetActuator.execute()`, which performs a taker's balance/asset deduction, order creation, and then iterates over up to `MAX_MATCH_NUM` maker orders in `matchOrder`/`matchSingleOrder` [1](#0-0) . Any single maker-order sub-step that throws (e.g. a missing/corrupted order fetched via `orderStore.get(orderId)`, or an arithmetic overflow in `MarketUtils.multiplyAndDivide`) propagates all the way up and aborts the entire `execute()` call via the checked `ItemNotFoundException`/`ContractValidateException` handling [2](#0-1) , causing the whole transaction (including the taker's own already-computed balance/asset adjustments) to fail.

### Finding Description
`MarketSellAssetActuator.matchOrder` walks a persistent, shared order book (`pairPriceToOrderStore` / `orderStore`) and calls `matchSingleOrder` for each maker order at the best price level: [3](#0-2) 

`matchSingleOrder` fetches the maker order with `orderStore.get(orderId)` [4](#0-3)  and declares `throws ItemNotFoundException`, meaning any maker order id present in `pairPriceToOrderStore`/`orderIdListCapsule` but not resolvable in `orderStore` (e.g. due to a state inconsistency from a prior bug, partial write, or fork-switch edge case) throws all the way out of `matchOrder` up into `execute()`. Because `execute()` only catches this at the top level and marks the whole contract `FAILED` (throwing `ContractExeException`) [2](#0-1) , this is functionally identical to the "child callback reverts, parent reverts" pattern in the report: a single bad entry in a shared, iterated list of "children" (maker orders) blocks the entire operation for every taker that would otherwise match against that price level, not merely for the corrupted maker.

Unlike a normal Solidity revert (`onReward` reverting the current call only), here the "poison" maker order remains permanently lodged in `pairPriceToOrderStore` at the head of that price's order list after the failure (the actuator's DB session change is entirely rolled back on exception, so the bad order is never removed) [5](#0-4) . Every subsequent taker attempting to match a sell order against that exact price pair will walk the same list, hit the same order id first (FIFO head), and fail identically — a deterministic, permanent denial of service for that specific trading pair/price level, effectively freezing the maker's listed assets (which can never be filled or cancelled through the matching path) and blocking takers from trading at that price.

### Impact Explanation
This maps to "permanent freezing of funds" for the affected market participants: the maker whose order is stuck can never have it filled (their listed sell-side tokens are effectively locked from ever executing), and every taker who would otherwise match at that exact price is unconditionally denied. Because `MAX_MATCH_NUM` bounds normal iteration, but a *single* early poisoned entry causes an unconditional throw rather than being skipped, the fault is not self-healing and requires the specific order (or, more critically, everything ahead of it in the queue) to be resolved out-of-band, which the protocol has no permissionless mechanism to do once the underlying `ItemNotFoundException` conditions exist. This is a Medium/High severity denial-of-funds issue reachable by any unprivileged account simply submitting an `AssetSellContract`-style order.

### Likelihood Explanation
Reaching this state requires an inconsistency between `pairPriceToOrderStore`/`orderIdListCapsule` and `orderStore` (i.e. an order id referenced in the price-index that is missing from the order store). I was not able to conclusively enumerate every code path that could produce such an inconsistency (this would require deeper review of `MarketOrderIdListCapsule.removeOrder`, deletion ordering, and rollback/fork-switch interactions in `Manager`), so likelihood is uncertain without further investigation; however, the reachability of the crash-and-halt-on-single-bad-entry logic itself is proven directly from the code and requires no special privilege to trigger — an attacker or even accidental state churn only needs to get the store into that inconsistent condition once.

### Recommendation
- In `matchOrder`/`matchSingleOrder`, do not let a missing/corrupt maker order abort the entire taker transaction. Skip and remove the offending entry from `pairPriceToOrderStore` (treating it as already consumed) instead of propagating `ItemNotFoundException` out of `execute()`.
- Add defensive validation/self-healing when the order-id-to-order mapping is inconsistent (log + prune) rather than throwing checked exceptions that fail the whole actuator.
- Add invariant/consistency checks between `orderStore` and `pairPriceToOrderStore` writes so deletions are always performed together, preventing dangling ids in the first place.

### Proof of Concept
A concrete, deterministic PoC requires constructing the order-book inconsistency (dangling order id in `pairPriceToOrderStore` without a corresponding `orderStore` entry), which I could not fully trace to a guaranteed root cause within the available context (index does not show the exact code path that would produce this desync, such as `MarketOrderIdListCapsule.removeOrder` internals or block-application rollback interplay with `orderStore`/`pairPriceToOrderStore` writes). Given the index size limits, some file contents (e.g. `MarketOrderIdListCapsule.java`, chain-base `MarketOrderStore.java` internals) were not fully retrievable; a full Devin session with complete repository access would be needed to construct and verify an exact reproduction transaction sequence.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L133-151)
```java
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
      accountStore.put(accountCapsule.createDbKey(), accountCapsule);

      ret.setOrderId(orderCapsule.getID());
      ret.setStatus(fee, code.SUCESS);
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

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L336-360)
```java
      byte[] pairPriceKey = priceKeysList.get(0);

      // if not exists
      MarketOrderIdListCapsule orderIdListCapsule = pairPriceToOrderStore.get(pairPriceKey);

      // match different orders which have the same price
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
