### Title
Premature hard revert in `MarketSellAssetActuator.matchOrder` discards all already-completed order matches instead of gracefully stopping - (File: `actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java`)

### Summary
The AMM.sol funding-update bug class is: a routine that iterates over a batch of independent, otherwise-ready state updates and, on encountering one entity that isn't ready/valid, hard-`revert`s the whole call instead of gracefully skipping/stopping, so unrelated ready updates are blocked. The closest reachable analog in java-tron is the maker-order matching loop inside `MarketSellAssetActuator.matchOrder`, which throws a `ContractValidateException` when an internal counter exceeds `MAX_MATCH_NUM`, aborting the whole transaction and discarding every match against independent maker orders that had already been computed in that same loop.

### Finding Description
`MarketSellAssetActuator.execute()` performs, in a single atomic actuator call: fee deduction, balance/token transfer, order creation, and `matchOrder()` [1](#0-0) .

`matchOrder()` walks price levels and, within each price level, walks the linked list of maker orders, calling `matchSingleOrder` for each one and incrementing `matchOrderCount`: [2](#0-1) 

Once `matchOrderCount` exceeds the static `MAX_MATCH_NUM` (default 20), the method throws:
```java
if (matchOrderCount > MAX_MATCH_NUM) {
  throw new ContractValidateException("Too many matches. MAX_MATCH_NUM = " + MAX_MATCH_NUM);
}
``` [3](#0-2) 

This exception propagates up through `execute()`'s catch block, which sets the receipt to `FAILED` and re-throws `ContractExeException`: [4](#0-3) 

Because none of `orderStore.put(...)` / `accountStore.put(...)` (the persistence calls) execute before the throw — they occur only after `matchOrder()` returns successfully — every maker order that was already matched inside the same loop iteration (i.e., orders belonging to other, unrelated, independent accounts that were each individually "ready" to be filled) is rolled back along with the taker's own order/balance changes, exactly mirroring the AMM bug: several independent, otherwise-valid state updates (each maker order) are blocked/undone purely because processing a batch happened to include one item that trips an internal limit.

The existing unit test confirms this exact behavior — a taker order that would legitimately match slightly more than `MAX_MATCH_NUM` resting orders causes the entire transaction to fail: [5](#0-4) 

### Impact Explanation
Any account that places a `MarketSellAssetContract` order whose size, given current order-book liquidity, would naturally cross more than `MAX_MATCH_NUM` (20) resting maker orders can never have that order filled — the transaction always reverts with "Too many matches," and the order is never partially filled or placed. This is directly analogous to the C4 finding: instead of gracefully capping the match count and persisting the matches already made (returning early, as the original report recommends), the actuator discards everything. An attacker can also deliberately fragment liquidity (place many small maker orders at slightly improving prices for a token pair) to force this condition, effectively denying any legitimate large taker order on that pair from ever executing — a liveness/DoS impact on the on-chain exchange for that trading pair. This qualifies as a Medium-severity availability issue reachable by any unprivileged order-placer via a standard signed `MarketSellAssetContract` transaction.

### Likelihood Explanation
High reachability: any account can submit a `MarketSellAssetContract` transaction; no special privilege is required, and the vulnerable condition is a routine outcome of normal, unremarkable order-book activity (many small maker orders at similar prices are common), or can be deliberately engineered by an attacker at low cost (placing many small maker orders).

### Recommendation
In `matchOrder`, replace the `throw new ContractValidateException("Too many matches...")` with logic that stops matching once `MAX_MATCH_NUM` is reached, keeps the matches already made, and treats the remaining unmatched `sellTokenQuantityRemain` as a new resting order (the same path already used when `hasMatch` returns null), instead of aborting the whole transaction and discarding valid state changes for unrelated maker orders.

### Proof of Concept
Using the existing test as the reproduction path: `MarketSellAssetActuatorTest.exceedMaxMatchNumLimit` seeds `MAX_MATCH_NUM + 1` maker orders and submits one sell order sized to match all of them; `actuator.execute()` throws `ContractExeException("Too many matches. MAX_MATCH_NUM = 20")`, and the transaction fails entirely rather than filling the first 20 matches and leaving the remainder as a new resting order. [5](#0-4) 

Note: I was unable to fully trace, within the remaining tool budget, exactly how `TransactionTrace`/block-application handles a `ContractExeException` thrown from an actuator (e.g., whether bandwidth/energy fee is still consumed for the reverted attempt, or whether the whole block containing this transaction is affected) — this would need further verification in `chainbase/src/main/java/org/tron/core/db/TransactionTrace.java` and `Manager.applyBlock`/`ActuatorExecutor` before finalizing severity beyond "Medium."

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

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L327-360)
```java
    int matchOrderCount = 0;
    // match different price
    while (takerCapsule.getSellTokenQuantityRemain() != 0) {
      // get lowest ordersList
      MarketPrice makerPrice = hasMatch(priceKeysList, takerPrice);
      if (makerPrice == null) {
        return;
      }

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

**File:** framework/src/test/java/org/tron/core/actuator/MarketSellAssetActuatorTest.java (L1825-1872)
```java
  @Test
  public void exceedMaxMatchNumLimit() throws Exception {

    InitAsset();

    int start = 10;
    int limit = MarketSellAssetActuator.getMAX_MATCH_NUM();
    int step = 1;
    int end = start + step * limit;

    //(sell id_1  and buy id_2)
    String sellTokenId = TOKEN_ID_ONE;
    String buyTokenId = TOKEN_ID_TWO;
    long buyTokenQuant = 400L;
    long sellTokenQuant = buyTokenQuant * (end / start + 1);

    byte[] ownerAddress = ByteArray.fromHexString(OWNER_ADDRESS_FIRST);
    AccountCapsule accountCapsule = dbManager.getAccountStore().get(ownerAddress);
    accountCapsule.addAssetAmountV2(sellTokenId.getBytes(), sellTokenQuant,
        dbManager.getDynamicPropertiesStore(), dbManager.getAssetIssueStore());
    dbManager.getAccountStore().put(ownerAddress, accountCapsule);
    Assert.assertEquals(sellTokenQuant,
            (long) accountCapsule.getAssetV2MapForTest().get(sellTokenId));

    // Initialize the order book

    // at least limit+1 times
    for (int i = start; i <= end; i += step) {
      addOrder(buyTokenId, (long) start, sellTokenId, i, OWNER_ADDRESS_SECOND);
    }

    // this order(taker) need to match 21 times
    MarketSellAssetActuator actuator = new MarketSellAssetActuator();
    actuator.setChainBaseManager(dbManager.getChainBaseManager()).setAny(getContract(
        OWNER_ADDRESS_FIRST, sellTokenId, sellTokenQuant, buyTokenId, buyTokenQuant));

    String errorMessage =
        "Too many matches. MAX_MATCH_NUM = " + MarketSellAssetActuator.getMAX_MATCH_NUM();
    try {
      TransactionResultCapsule ret = new TransactionResultCapsule();
      actuator.validate();
      actuator.execute(ret);
      fail(errorMessage);
    } catch (ContractExeException e) {
      Assert.assertEquals(errorMessage, e.getMessage());
    } catch (Exception e) {
      Assert.assertTrue(false);
    }
```
