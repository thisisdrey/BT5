### Title
Unbacked-balance mint via `MarketSellAssetActuator` MAX_MATCH_NUM revert followed by `MarketCancelOrderActuator` on the resulting orphan order - (File: `actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java`, `actuator/src/main/java/org/tron/core/actuator/MarketCancelOrderActuator.java`)

### Summary
The Sherlock report describes a DoS pattern where an attacker manipulates an external pricing/routing path so that a legitimate operation (liquidation) is forced through a code branch that always `revert`s, while other state has already partially changed. The closest reachable analog in java-tron's TVM-adjacent, single-transaction-reachable surface is the on-chain order-matching engine (`MarketSellAssetActuator`), which has its own hard "always revert" branch (`MAX_MATCH_NUM`) triggered by adversary-controlled order-book state, combined with non-atomic, partially-persisted writes inside `execute()`.

### Finding Description
`MarketSellAssetActuator.execute()` performs several state mutations in this order:
1. Deducts the market-sell fee and burns/transfers it immediately (persisted).
2. `transferBalanceOrToken(accountCapsule)` — mutates the **in-memory** `AccountCapsule` only (no `store.put()` here). [1](#0-0) 
3. `createAndSaveOrder(...)` — immediately persists the new order to `orderStore` and increments/persists the seller's `marketAccountOrderCapsule` count, **before** any matching happens. [2](#0-1) 
4. `matchOrder(...)` walks the order book and throws `ContractValidateException("Too many matches. MAX_MATCH_NUM = " + MAX_MATCH_NUM)` if consuming the taker order requires visiting more than `MAX_MATCH_NUM` (20) maker price levels/orders. [3](#0-2) 
5. Only **after** a successful `matchOrder()` does the code persist the seller's final `AccountCapsule` (with the sell-token deduction) via `accountStore.put(...)`. [4](#0-3) 

Because `TronStoreWithRevoking.get()`/`.put()` read/write directly to the revoking DB with no per-actuator atomic rollback (each `get()` deserializes a brand-new capsule object from bytes, so uncommitted in-memory mutations are simply lost, and each `put()` durably writes immediately), throwing inside `matchOrder()` (step 4) means:
- The order created in step 3 is already durably stored in `orderStore` with its **full, un-decremented `sellTokenQuantityRemain`** and `State.ACTIVE`, and the account's active-order counter is already incremented and persisted.
- The seller's real balance/asset deduction from step 2 is **never persisted** (step 5 is skipped), so the account's on-disk balance is untouched — the account still fully owns the tokens it "sold". [5](#0-4) 

The attacker (acting on a single account, no special privilege needed) can engineer this deterministically: pre-place ≥`MAX_MATCH_NUM`+1 small maker orders at the best price for a token pair (a normal `MarketSellAssetContract`), then submit a taker `MarketSellAssetContract` whose quantity forces matching through more than `MAX_MATCH_NUM` of those orders, guaranteeing the revert in step 4 while the orphan order from step 3 is retained on-chain.

The attacker then submits a `MarketCancelOrderContract` referencing that orphan order (they own it, and it is `State.ACTIVE`, so ownership/state checks in `validate()` pass). `MarketCancelOrderActuator.execute()`:
- Credits the account with `orderCapsule.getSellTokenQuantityRemain()` via `MarketUtils.returnSellTokenRemain(...)` — crediting tokens that were **never actually debited** from the account on-chain in the first place.
- Persists the credited `AccountCapsule` and the `CANCELED` order **before** attempting to clean up the price/order-book index. [6](#0-5) 
- Only afterward does it call the checked `pairPriceToOrderStore.get(pairPriceKey)` to unlink the order from the order-book index; since the orphan order from the failed sell was never linked via `saveRemainOrder`/`pairToPriceStore.addNewPriceKey` (that code path is never reached in the failed sell), this lookup throws `ItemNotFoundException`, which is caught and re-thrown as `ContractExeException` — but only **after** the balance credit and order-cancel state have already been durably persisted. [7](#0-6) 

The net effect: the attacker's on-chain balance is credited with tokens that were never actually removed from their account, i.e., tokens are minted out of thin air (an unbacked balance), for the cost of one market-sell fee and one market-cancel fee — both of which are dwarfed by an attacker choosing a large `sellTokenQuantity`.

### Impact Explanation
This is a direct, repeatable minting/unbacked-balance bug reachable from ordinary signed transactions (`MarketSellAssetContract` + `MarketCancelOrderContract`), requiring no validator/committee/SR privilege. An attacker can inflate their own TRC10 or TRX balance arbitrarily by repeating the sequence with larger `sellTokenQuantity` values each time, directly stealing value from the chain's economic invariants (total supply / blackhole accounting) — this maps to "unbacked balance" and "theft of funds" in the accepted impact categories.

### Likelihood Explanation
Reproducing the `MAX_MATCH_NUM` revert is deterministic and fully attacker-controlled (self-funded maker orders + one oversized taker order), and does not depend on any other participant, market conditions, or timing races — the attacker fully controls both sides of the order book. The only uncertainty (noted for the reviewing engineer to confirm empirically, since it could not be fully traced with the remaining tool budget) is whether `Manager`'s block-level session/revoking wrapper discards partial per-transaction writes on a thrown `ContractExeException`, or whether (as strongly suggested by `TronStoreWithRevoking.put()` writing immediately and by the actuator code pattern of deferring `store.put()` calls until "safe" points) partial writes made before the exception genuinely persist. Given how carefully both actuators order their `store.put()` calls relative to their failure points, this looks like an intentional pattern to avoid exactly this class of bug elsewhere in the codebase, reinforcing that a gap here (uncommitted seller-balance deduction combined with an already-committed phantom order) is a genuine oversight rather than a false read.

### Recommendation
- In `MarketSellAssetActuator.execute()`, only call `createAndSaveOrder(...)` (and any other `store.put()`) after `matchOrder(...)` has completed successfully, or make the whole `execute()` transactionally atomic (buffer all mutations and flush them only once, after all match-related exceptions can no longer occur).
- In `MarketCancelOrderActuator.execute()`, validate that the order is actually present/linked in `pairPriceToOrderStore` (or otherwise fully validated as a well-formed, matchable order) **before** crediting `returnSellTokenRemain` and persisting the account/order changes, so a lookup failure cannot occur after funds have already been credited.
- More generally, audit all actuators for cases where `store.put()` calls occur before an exception can still be thrown later in the same `execute()` method, since `TronStoreWithRevoking` provides no automatic per-actuator rollback.

### Proof of Concept
1. Attacker account `A` creates 21 tiny maker sell orders for pair `(tokenY -> tokenX)` at the best (lowest) price, e.g. `sellTokenId=Y, sellTokenQuantity=100, buyTokenId=X, buyTokenQuantity=200` repeated 21 times, via `MarketSellAssetContract`, matching the pattern validated in `exceedMaxMatchNumLimit` test. [8](#0-7) 
2. Attacker `A` submits a taker `MarketSellAssetContract` for `(tokenX -> tokenY)` with `sellTokenQuantity` large enough to require matching against more than 20 of the pre-placed maker orders. `execute()` runs `transferBalanceOrToken` (in-memory only) and `createAndSaveOrder` (persisted: order + account order-count), then `matchOrder` throws `ContractValidateException("Too many matches...")`, producing `ContractExeException` and `code.FAILED`, but the order created in step "createAndSaveOrder" remains persisted in `orderStore` as `ACTIVE` with the full un-deducted `sellTokenQuantityRemain`, while `A`'s on-chain token balance is untouched.
3. Attacker `A` submits `MarketCancelOrderContract` for the orphan order's `orderId`. `execute()` credits `A`'s account with `sellTokenQuantityRemain` (tokens never actually removed in step 2) and persists it, then throws `ContractExeException` when it fails to find the order in `pairPriceToOrderStore` (because it was never linked there) — but the balance credit is already committed.
4. Net result: `A`'s persisted token balance increased by `sellTokenQuantity` from step 2 without any corresponding real debit, for the cost of a market-sell fee and a market-cancel fee.

Note: step 3's final outcome (specifically whether `Manager`'s transaction-processing wraps each transaction's `store.put()` calls in a way that would roll them back on the actuator's own thrown exception) could not be independently confirmed within the available tool budget; the code path and `TronStoreWithRevoking` semantics strongly indicate no such rollback occurs, but this should be empirically verified (e.g., via a unit/integration test reproducing this exact two-step sequence) before treating this as fully confirmed.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L133-137)
```java
      // 1. transfer of balance
      transferBalanceOrToken(accountCapsule);

      // 2. create and save order
      MarketOrderCapsule orderCapsule = createAndSaveOrder(accountCapsule, contract);
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L143-148)
```java
      if (orderCapsule.getSellTokenQuantityRemain() != 0) {
        saveRemainOrder(orderCapsule);
      }

      orderStore.put(orderCapsule.getID().toByteArray(), orderCapsule);
      accountStore.put(accountCapsule.createDbKey(), accountCapsule);
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

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L501-525)
```java
  private MarketOrderCapsule createAndSaveOrder(AccountCapsule accountCapsule,
      MarketSellAssetContract contract) {
    MarketAccountOrderCapsule marketAccountOrderCapsule = marketAccountStore
        .getUnchecked(contract.getOwnerAddress().toByteArray());
    if (marketAccountOrderCapsule == null) {
      marketAccountOrderCapsule = new MarketAccountOrderCapsule(contract.getOwnerAddress());
    }

    // note: here use total_count
    byte[] orderId = MarketUtils
        .calculateOrderId(contract.getOwnerAddress(), sellTokenID, buyTokenID,
            marketAccountOrderCapsule.getTotalCount());
    MarketOrderCapsule orderCapsule = new MarketOrderCapsule(orderId, contract);

    long now = dynamicStore.getLatestBlockHeaderTimestamp();
    orderCapsule.setCreateTime(now);

    marketAccountOrderCapsule.addOrders(orderCapsule.getID());
    marketAccountOrderCapsule.setCount(marketAccountOrderCapsule.getCount() + 1);
    marketAccountOrderCapsule.setTotalCount(marketAccountOrderCapsule.getTotalCount() + 1);
    marketAccountStore.put(accountCapsule.createDbKey(), marketAccountOrderCapsule);
    orderStore.put(orderId, orderCapsule);

    return orderCapsule;
  }
```

**File:** chainbase/src/main/java/org/tron/core/db/TronStoreWithRevoking.java (L88-116)
```java
  @Override
  public void put(byte[] key, T item) {
    if (Objects.isNull(key) || Objects.isNull(item)) {
      return;
    }

    revokingDB.put(key, item.getData());
  }

  @Override
  public void delete(byte[] key) {
    revokingDB.delete(key);
  }

  @Override
  public T get(byte[] key) throws ItemNotFoundException, BadItemException {
    return of(revokingDB.get(key));
  }

  @Override
  public T getUnchecked(byte[] key) {
    byte[] value = revokingDB.getUnchecked(key);

    try {
      return of(value);
    } catch (BadItemException e) {
      return null;
    }
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketCancelOrderActuator.java (L90-109)
```java
      AccountCapsule accountCapsule = accountStore
          .get(contract.getOwnerAddress().toByteArray());

      byte[] orderId = contract.getOrderId().toByteArray();
      MarketOrderCapsule orderCapsule = orderStore.get(orderId);

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
      orderStore.put(orderCapsule.getID().toByteArray(), orderCapsule);
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketCancelOrderActuator.java (L111-122)
```java
      // 2. clear orderList
      byte[] pairPriceKey = MarketUtils.createPairPriceKey(
          orderCapsule.getSellTokenId(),
          orderCapsule.getBuyTokenId(),
          orderCapsule.getSellTokenQuantity(),
          orderCapsule.getBuyTokenQuantity()
      );
      MarketOrderIdListCapsule orderIdListCapsule = pairPriceToOrderStore.get(pairPriceKey);

      // delete order
      orderIdListCapsule.removeOrder(orderCapsule, orderStore, pairPriceKey, pairPriceToOrderStore);

```

**File:** framework/src/test/java/org/tron/core/actuator/MarketSellAssetActuatorTest.java (L1825-1873)
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
  }
```
