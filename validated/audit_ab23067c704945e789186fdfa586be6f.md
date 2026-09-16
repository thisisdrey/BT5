Good — cancellation exists, so this confirms makers can free their funds despite the DoS, but the matching function itself remains permanently unusable for larger takers against that price level. This is enough evidence to finalize the analog.

### Title
Malicious market makers can grief order matching by exceeding `MAX_MATCH_NUM`, permanently blocking legitimate taker orders from executing - (File: `actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java`)

### Summary
The TRON on-chain DEX (`MarketSellAssetActuator`) matches a taker's sell order against resting maker orders at the best price level in a `while` loop. If more than `MAX_MATCH_NUM` (20) matches would be required to satisfy one taker transaction, the actuator throws a `ContractValidateException` from deep inside `matchOrder()`, which propagates out of `execute()` as a `ContractExeException`, aborting the whole transaction [1](#0-0) . This mirrors the reported bug class: state set by one set of users (here, many small resting orders placed by a malicious market participant) later causes a hard revert of a legitimate, unrelated operation (a taker trying to fill/rebalance against the order book) performed deep within a nested call, and there is no way for the taker to work around the check other than shrinking their order.

### Finding Description
`matchOrder()` walks the maker order list for the best price and calls `matchSingleOrder()` repeatedly; each successful partial/whole fill increments `matchOrderCount`, and once it exceeds the constant `MAX_MATCH_NUM = 20`, the method throws `ContractValidateException("Too many matches. MAX_MATCH_NUM = " + MAX_MATCH_NUM)` [2](#0-1) . This check lives inside `execute()`, not `validate()`, so it is only discovered after the actuator has already begun mutating state (fee burn, `transferBalanceOrToken`, `createAndSaveOrder`) [3](#0-2) . The exception is caught in `execute()`'s catch clause, which sets the result to `FAILED` and rethrows as `ContractExeException`, causing the entire transaction (and its revoking-db session) to be rolled back, exactly analogous to how `setDeltaAllocationsInt()`'s `require(!blacklist)` check aborts `receiveProtocolAllocations()` deep in the call chain in the original report.

Because there is no way to skip poisoned/excess maker orders — the loop must consume orders strictly in best-price order and any single execution attempt is capped at `MAX_MATCH_NUM` matches — a malicious actor can place more than 20 small orders at (or fragment liquidity into more than 20 orders at) the best price for a token pair. Any legitimate taker whose order size requires matching more than 20 of those resting orders will have their transaction unconditionally fail every time they attempt it, since match state never partially persists (the whole session reverts) and the offending resting orders remain untouched in the order book. The `MarketCancelOrderActuator` lets individual order owners cancel their own orders [4](#0-3) , but a taker has no ability to force-remove or skip a malicious counterparty's spam orders, so the specific price level is effectively "poisoned" against large takers until the malicious maker chooses to withdraw.

### Impact Explanation
This allows a griefer to permanently deny large trades from being executed against a given price level of the on-chain exchange by fragmenting liquidity into many small orders, without requiring any special privilege — any account can place `MarketSellAssetContract` transactions. While it does not directly steal funds (the taker's transaction reverts entirely, and the maker can eventually cancel), it constitutes a persistent denial-of-service on the exchange/market-order matching functionality for that trading pair, which is one of the explicitly in-scope broadcastable actuator paths.

### Likelihood Explanation
Likelihood is high: placing many small `MarketSellAssetContract` orders at the same price is trivial and only costs the standard market-sell fee per order; no special permissions or large capital are required to fragment a price level into more than `MAX_MATCH_NUM` orders.

### Recommendation
Rather than reverting the whole transaction when `MAX_MATCH_NUM` is exceeded, the actuator should stop matching at the limit and persist the partial fill (leaving the remainder of the taker's order in the book or returning the unmatched remainder), instead of throwing an exception that unwinds all already-computed matches for that transaction. Alternatively, allow takers to specify/paginate matches across multiple transactions, or bound the number of maker orders a single account/price level can create to prevent order fragmentation.

### Proof of Concept
1. Attacker places 21 `MarketSellAssetContract` orders selling a small quantity of token A for token B at the identical (best) price.
2. A legitimate user submits a `MarketSellAssetContract` order that needs to match against all resting orders at that price level to be filled to the desired amount.
3. Inside `execute()` → `matchOrder()`, the 21st match increments `matchOrderCount` past `MAX_MATCH_NUM` and throws `ContractValidateException("Too many matches...")` [5](#0-4) ; this is confirmed by the existing test `exceedMaxMatchNumLimit` which reproduces exactly this failure [6](#0-5) .
4. The victim's transaction fails every time they retry with the same size, and the attacker's spam orders remain in the book (until voluntarily canceled), so any comparable-sized order will keep failing at that price level.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L100-145)
```java
  public boolean execute(Object object) throws ContractExeException {
    initStores();

    TransactionResultCapsule ret = (TransactionResultCapsule) object;
    if (Objects.isNull(ret)) {
      throw new RuntimeException(TX_RESULT_NULL);
    }

    long fee = calcFee();

    try {
      final MarketSellAssetContract contract = this.any
          .unpack(MarketSellAssetContract.class);

      AccountCapsule accountCapsule = accountStore
          .get(contract.getOwnerAddress().toByteArray());

      sellTokenID = contract.getSellTokenId().toByteArray();
      buyTokenID = contract.getBuyTokenId().toByteArray();
      sellTokenQuantity = contract.getSellTokenQuantity();
      buyTokenQuantity = contract.getBuyTokenQuantity();
      MarketPrice takerPrice = MarketPrice.newBuilder()
          .setSellTokenQuantity(sellTokenQuantity)
          .setBuyTokenQuantity(buyTokenQuantity).build();

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
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L329-360)
```java
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

**File:** actuator/src/main/java/org/tron/core/actuator/MarketCancelOrderActuator.java (L75-148)
```java
  @Override
  public boolean execute(Object object) throws ContractExeException {

    initStores();

    TransactionResultCapsule ret = (TransactionResultCapsule) object;
    if (Objects.isNull(ret)) {
      throw new RuntimeException(TX_RESULT_NULL);
    }
    long fee = calcFee();

    try {
      final MarketCancelOrderContract contract = this.any
          .unpack(MarketCancelOrderContract.class);

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

      if (orderIdListCapsule.isOrderEmpty()) {
        // if orderList is empty, delete
        pairPriceToOrderStore.delete(pairPriceKey);

        // 3. modify priceList
        // decrease price number
        // if empty, delete token pair
        byte[] makerPair = MarketUtils
            .createPairKey(orderCapsule.getSellTokenId(), orderCapsule.getBuyTokenId());
        long remainCount = pairToPriceStore.getPriceNum(makerPair) - 1;
        if (remainCount == 0) {
          pairToPriceStore.delete(makerPair);
        } else {
          pairToPriceStore.setPriceNum(makerPair, remainCount);
        }
      }

      ret.setStatus(fee, code.SUCESS);
    } catch (ItemNotFoundException
        | InvalidProtocolBufferException
        | BalanceInsufficientException e) {
      logger.debug(e.getMessage(), e);
      ret.setStatus(fee, code.FAILED);
      throw new ContractExeException(e.getMessage());
    }
    return true;
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
