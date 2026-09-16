### Title
Uncaught `ArithmeticException` in `MarketSellAssetActuator.execute()` order matching leaves partial, unrolled-back state after `validate()` already accepted the order — ([File: actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java])

### Summary
The C4 finding is a class of bug where a value that was checked against a limit *before* a reward/adjustment is applied is later checked (or used) *after* the adjustment, so a transaction that passed the earlier check can fail the later one, causing loss of funds because the state changes made before the failure are not rolled back. The closest reachable analog in java-tron is `MarketSellAssetActuator`, where `validate()` performs its numeric/overflow checks wrapped in a `try { ... } catch (ArithmeticException e)` block [1](#0-0) , but `execute()` — which performs the actual order matching arithmetic (`multiplyAndDivide`, `addExact`, `subtractExact` in `matchOrder`/`matchSingleOrder`) — only catches `ItemNotFoundException | InvalidProtocolBufferException | BalanceInsufficientException | ContractValidateException`, NOT `ArithmeticException` [2](#0-1) .

### Finding Description
`MarketSellAssetActuator.execute()` mutates state directly against the live stores (not through a rollback-capable repository session) in this order: it debits the fee and blackhole balance, then calls `transferBalanceOrToken` (deduct seller balance/asset), `createAndSaveOrder` (persists order to `orderStore`/`marketAccountStore`), and finally `matchOrder` [3](#0-2) .

`matchOrder`/`matchSingleOrder` recompute quantities using `MarketUtils.multiplyAndDivide`, `addExact`, and `subtractExact` against *maker* orders that were created and validated in **earlier, unrelated transactions** [4](#0-3) . Each of those maker orders passed its own `validate()` at creation time (checked against the account's balance/asset at that point), but by the time a taker's transaction triggers matching, the arithmetic combines fresh taker values with stored maker values that were never re-validated together. If this combined arithmetic overflows (`addExact`/`subtractExact`/`multiplyAndDivide` all throw `ArithmeticException` on overflow), the exception is not declared or caught in `execute()`'s catch clause, so it propagates as an unchecked exception — after `orderStore.put(makerOrderCapsule...)` and partial account balance mutations for some matched makers have already been written directly to the store within the same loop (line 486 `orderStore.put` happens per iteration, inside the `while` loop, before the loop even completes) [5](#0-4) .

This exactly mirrors the report's root cause: a check/guard performed at one point in time (order/maker validation) is inconsistent with the value actually used at settlement time (matching arithmetic combining live-updated quantities), and the mismatch is only detected too late — after irreversible state mutations for some counterparties have already been persisted.

### Impact Explanation
If order matching triggers an `ArithmeticException` mid-loop, some `MarketOrderCapsule` and account balance/asset writes for earlier-matched makers in the same `matchOrder` loop are already persisted to `orderStore`/`accountStore` (since java-tron actuators write directly to `Store` objects rather than a repository savepoint that reverts atomically), while the taker's own top-level transaction fails with an uncaught exception. This can produce inconsistent account/order state (assets debited/credited for some makers without the corresponding taker leg completing consistently), and, depending on how `Manager`/block application handles an uncaught `RuntimeException` from `actuator.execute()`, could also crash or halt processing of the block for all nodes replaying the transaction — a potential chain-halt condition, or a permanent balance/asset inconsistency for the affected accounts.

### Likelihood Explanation
Triggering an `ArithmeticException` requires crafting maker/taker order quantities that cause `addExact`/`subtractExact`/`multiplyAndDivide` to overflow `long` bounds — reachable by any unprivileged account by placing sell orders with `MarketSellAssetContract` (via `MarketSellAssetActuator`), since `sellTokenQuantity`/`buyTokenQuantity` are checked against `getMarketQuantityLimit()` in `validate()` but the *quantity limit* check is on individual order values, not on the compounded overflow scenarios produced during multi-order matching over time. Producing the specific overflow requires careful crafting of quantities/prices across multiple orders, so likelihood is Medium rather than High.

### Recommendation
1. In `MarketSellAssetActuator.execute()`, wrap the order-matching logic (`matchOrder`) in a try/catch that also handles `ArithmeticException`, converting it into a `ContractExeException` so it is caught by the existing catch clause and properly reported/rolled back rather than propagating as an unchecked exception.
2. Ensure that order matching state changes (`orderStore.put`, account balance/asset updates within `matchOrder`) are performed within a transactional/rollback-capable session so a mid-loop failure does not leave partially-applied state.
3. Add defensive bounds re-validation inside `matchSingleOrder` before performing the exact-arithmetic operations, so an overflow condition is detected and safely handled (e.g., abort the specific match rather than throwing an unhandled exception) instead of relying solely on `validate()`-time checks that do not account for state accumulated from other, previously placed orders.

### Proof of Concept
Not fully verifiable via static code reading alone — exploiting this requires constructing a sequence of `MarketSellAssetContract` orders whose sell/buy quantities and existing order-book prices, when combined during `matchSingleOrder`'s `multiplyAndDivide`/`addExact`/`subtractExact` calls, exceed `Long.MAX_VALUE`/underflow `Long.MIN_VALUE`. This requires runtime testing (e.g., extending the existing test `MarketSellAssetActuatorTest` scenarios such as `matchAll2SamePriceBuyOrders1` or `partMatchMakerLeftNotEnoughBuyOrders1` [6](#0-5) ) with extreme quantity values near `Long.MAX_VALUE` to force an `ArithmeticException` inside `matchOrder` and observe whether execution throws uncaught out of `MarketSellAssetActuator.execute()` and whether prior `orderStore`/`accountStore` writes from the same call persist. I was not able to execute this in the current environment; a Devin session with build/test tooling would be needed to confirm the exact runtime behavior (uncaught propagation and store persistence semantics) with certainty.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L108-151)
```java
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

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L241-279)
```java
    try {
      // Whether the balance is enough
      long fee = calcFee();

      if (Arrays.equals(sellTokenID, "_".getBytes())) {
        if (ownerAccount.getBalance() < addExact(sellTokenQuantity, fee)) {
          throw new ContractValidateException("No enough balance !");
        }
      } else {
        if (ownerAccount.getBalance() < fee) {
          throw new ContractValidateException("No enough balance !");
        }

        AssetIssueCapsule assetIssueCapsule = Commons
            .getAssetIssueStoreFinal(dynamicStore, assetIssueStore, assetIssueV2Store)
            .get(sellTokenID);
        if (assetIssueCapsule == null) {
          throw new ContractValidateException("No sellTokenId !");
        }
        if (!ownerAccount.assetBalanceEnoughV2(sellTokenID, sellTokenQuantity,
            dynamicStore)) {
          throw new ContractValidateException("SellToken balance is not enough !");
        }
      }

      if (!Arrays.equals(buyTokenID, "_".getBytes())) {
        // Whether have the token
        AssetIssueCapsule assetIssueCapsule = Commons
            .getAssetIssueStoreFinal(dynamicStore, assetIssueStore, assetIssueV2Store)
            .get(buyTokenID);
        if (assetIssueCapsule == null) {
          throw new ContractValidateException("No buyTokenId !");
        }
      }
    } catch (ArithmeticException e) {
      logger.debug(e.getMessage(), e);
      throw new ContractValidateException(e.getMessage());
    }

```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L307-360)
```java
  private void matchOrder(MarketOrderCapsule takerCapsule, MarketPrice takerPrice,
      TransactionResultCapsule ret, AccountCapsule takerAccountCapsule)
      throws ItemNotFoundException, ContractValidateException {

    byte[] makerSellTokenID = buyTokenID;
    byte[] makerBuyTokenID = sellTokenID;
    byte[] makerPair = MarketUtils.createPairKey(makerSellTokenID, makerBuyTokenID);

    // makerPair not exists
    long makerPriceNumber = pairToPriceStore.getPriceNum(makerPair);
    if (makerPriceNumber == 0) {
      return;
    }
    long remainCount = makerPriceNumber;

    // get maker price list
    List<byte[]> priceKeysList = pairPriceToOrderStore
        .getPriceKeysList(MarketUtils.getPairPriceHeadKey(makerSellTokenID, makerBuyTokenID),
            (long) (MAX_MATCH_NUM + 1), makerPriceNumber, true);

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

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L383-483)
```java
  private void matchSingleOrder(MarketOrderCapsule takerOrderCapsule,
      MarketOrderCapsule makerOrderCapsule, TransactionResultCapsule ret,
      AccountCapsule takerAccountCapsule)
      throws ItemNotFoundException {

    long takerSellRemainQuantity = takerOrderCapsule.getSellTokenQuantityRemain();
    long makerSellQuantity = makerOrderCapsule.getSellTokenQuantity();
    long makerBuyQuantity = makerOrderCapsule.getBuyTokenQuantity();
    long makerSellRemainQuantity = makerOrderCapsule.getSellTokenQuantityRemain();

    // according to the price of maker, calculate the quantity of taker can buy
    // for makerPrice,sellToken is A,buyToken is TRX.
    // for takerPrice,buyToken is A,sellToken is TRX.

    // makerSellTokenQuantity_A/makerBuyTokenQuantity_TRX =
    //   takerBuyTokenQuantityCurrent_A/takerSellTokenQuantityRemain_TRX
    // => takerBuyTokenQuantityCurrent_A = takerSellTokenQuantityRemain_TRX *
    //   makerSellTokenQuantity_A/makerBuyTokenQuantity_TRX

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

**File:** framework/src/test/java/org/tron/core/actuator/MarketSellAssetActuatorTest.java (L1296-1385)
```java
  @Test
  public void matchAll2SamePriceBuyOrders1() throws Exception {

    InitAsset();

    //(sell id_1  and buy id_2)
    String sellTokenId = TOKEN_ID_ONE;
    long sellTokenQuant = 400L;
    String buyTokenId = TOKEN_ID_TWO;
    long buyTokenQuant = 200L;

    byte[] ownerAddress = ByteArray.fromHexString(OWNER_ADDRESS_FIRST);
    AccountCapsule accountCapsule = dbManager.getAccountStore().get(ownerAddress);
    accountCapsule.addAssetAmountV2(sellTokenId.getBytes(), sellTokenQuant,
        dbManager.getDynamicPropertiesStore(), dbManager.getAssetIssueStore());
    dbManager.getAccountStore().put(ownerAddress, accountCapsule);
    Assert.assertEquals(sellTokenQuant,
            (long) accountCapsule.getAssetV2MapForTest().get(sellTokenId));

    // Initialize the order book

    //add three order(sell id_2 and buy id_1) with different price by the same account
    //TOKEN_ID_TWO is twice as expensive as TOKEN_ID_ONE
    addOrder(TOKEN_ID_TWO, 100L, TOKEN_ID_ONE,
        200L, OWNER_ADDRESS_SECOND);
    addOrder(TOKEN_ID_TWO, 100L, TOKEN_ID_ONE,
        200L, OWNER_ADDRESS_SECOND);
    addOrder(TOKEN_ID_TWO, 100L, TOKEN_ID_ONE,
        300L, OWNER_ADDRESS_SECOND);

    //add three order(sell id_1  and buy id_2)
    //TOKEN_ID_ONE is twice as expensive as TOKEN_ID_TWO
    addOrder(TOKEN_ID_ONE, 100L, TOKEN_ID_TWO,
        200L, OWNER_ADDRESS_SECOND);
    addOrder(TOKEN_ID_ONE, 100L, TOKEN_ID_TWO,
        300L, OWNER_ADDRESS_SECOND);
    addOrder(TOKEN_ID_ONE, 100L, TOKEN_ID_TWO,
        400L, OWNER_ADDRESS_SECOND);

    // do process
    MarketSellAssetActuator actuator = new MarketSellAssetActuator();
    actuator.setChainBaseManager(dbManager.getChainBaseManager()).setAny(getContract(
        OWNER_ADDRESS_FIRST, sellTokenId, sellTokenQuant, buyTokenId, buyTokenQuant));

    TransactionResultCapsule ret = new TransactionResultCapsule();
    actuator.validate();
    actuator.execute(ret);

    //get storeDB instance
    ChainBaseManager chainBaseManager = dbManager.getChainBaseManager();
    MarketAccountStore marketAccountStore = chainBaseManager.getMarketAccountStore();
    MarketOrderStore orderStore = chainBaseManager.getMarketOrderStore();
    MarketPairToPriceStore pairToPriceStore = chainBaseManager.getMarketPairToPriceStore();
    MarketPairPriceToOrderStore pairPriceToOrderStore = chainBaseManager
        .getMarketPairPriceToOrderStore();

    //check balance and token
    accountCapsule = dbManager.getAccountStore().get(ownerAddress);
    Assert.assertEquals(0L,
            (long) accountCapsule.getAssetV2MapForTest().get(sellTokenId));
    Assert.assertEquals(200L,
            (long) accountCapsule.getAssetV2MapForTest().get(buyTokenId));

    byte[] makerAddress = ByteArray.fromHexString(OWNER_ADDRESS_SECOND);
    AccountCapsule makerAccountCapsule = dbManager.getAccountStore().get(makerAddress);
    Assert.assertEquals(400L,
            (long) makerAccountCapsule.getAssetV2MapForTest().get(sellTokenId));

    //check accountOrder
    MarketAccountOrderCapsule accountOrderCapsule = marketAccountStore.get(ownerAddress);
    Assert.assertEquals(0, accountOrderCapsule.getCount());
    // ByteString orderId = accountOrderCapsule.getOrdersList().get(0);

    MarketAccountOrderCapsule makerAccountOrderCapsule = marketAccountStore.get(makerAddress);
    Assert.assertEquals(4, makerAccountOrderCapsule.getCount());
    // ByteString makerOrderId1 = makerAccountOrderCapsule.getOrdersList().get(0);
    // ByteString makerOrderId2 = makerAccountOrderCapsule.getOrdersList().get(1);

    //check order
    // MarketOrderCapsule orderCapsule = orderStore.get(orderId.toByteArray());
    // Assert.assertEquals(0L, orderCapsule.getSellTokenQuantityRemain());
    // Assert.assertEquals(State.INACTIVE, orderCapsule.getSt());

    // MarketOrderCapsule makerOrderCapsule1 = orderStore.get(makerOrderId1.toByteArray());
    // Assert.assertEquals(0L, makerOrderCapsule1.getSellTokenQuantityRemain());
    // Assert.assertEquals(State.INACTIVE, makerOrderCapsule1.getSt());

    // MarketOrderCapsule makerOrderCapsule2 = orderStore.get(makerOrderId2.toByteArray());
    // Assert.assertEquals(0L, makerOrderCapsule2.getSellTokenQuantityRemain());
    // Assert.assertEquals(State.INACTIVE, makerOrderCapsule2.getSt());
```
