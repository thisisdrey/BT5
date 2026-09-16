### Title
Market order book can be permanently DoS'ed via cheap dust orders exceeding `MAX_MATCH_NUM` in matching - (File: `actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java`)

### Summary
`MarketSellAssetActuator.matchOrder()` aborts a taker's trade with a hard exception once it has matched against more than `MAX_MATCH_NUM` (20) maker orders at the same or better price. An unprivileged account can flood a trading pair's order book with many minimal-size orders at closely-spaced prices; any subsequent legitimate `MarketSellAssetContract` transaction that needs to walk through that many price levels/orders will always exceed the cap and revert, permanently denying trading on that pair for anyone whose order would otherwise match more than 20 makers — analogous to the Halborn auction report where a tiny, cheap bid froze the market for everyone else.

### Finding Description
The market order matching loop increments `matchOrderCount` for every maker order consumed and throws a `ContractValidateException` once the count passes `MAX_MATCH_NUM`: [1](#0-0) 

`MAX_MATCH_NUM` is a fixed constant (20), and the only per-account cap on outstanding orders is `MAX_ACTIVE_ORDER_NUM` (100), which does not prevent an attacker from placing dozens of tiny orders across one or more accounts: [2](#0-1) [3](#0-2) 

There is no minimum order size or price-clustering restriction enforced in `validate()` beyond `sellTokenQuantity > 0` and the pair-wide `MarketQuantityLimit`, so an attacker can create many orders each selling as little as `1` unit at slightly different prices: [4](#0-3) 

Because `execute()` runs the fee deduction, balance transfer, order creation, and then `matchOrder()` in sequence, any victim transaction that needs to traverse more than 20 dust orders throws inside `matchOrder()`, and the whole contract execution fails: [5](#0-4) 

This mirrors the reported `AuctionUpgradeable` bug class: a cheap, small-value action (tiny dust orders here, a tiny extreme bid there) permanently blocks legitimate participants (bidders/takers) from completing their otherwise valid transactions, at negligible cost to the attacker.

### Impact Explanation
Any account whose sell order would need to match more than `MAX_MATCH_NUM` maker orders to fill (a very common case for reasonably sized trades against a thin order book) will have every attempt revert as long as the attacker maintains ≥21 small orders spread across the relevant price range for that pair. This denies the `MarketSellAssetContract` trading path (an unprivileged, order-placer-reachable API) for that token pair, a persistent availability/DoS impact on the exchange/market order handling functionality explicitly in scope.

### Likelihood Explanation
Likelihood is high: creating many minimal-quantity orders only costs the per-order `MarketSellFee` (`calcFee()`) and negligible token amounts (as low as 1 unit each), and can be split across freely creatable accounts to bypass the 100-orders-per-account (`MAX_ACTIVE_ORDER_NUM`) limit. No special privileges are required — only the ability to submit ordinary `MarketSellAssetContract` transactions. [6](#0-5) 

### Recommendation
Consider either removing the hard revert on exceeding `MAX_MATCH_NUM` in favor of partially filling and leaving the remainder on the book (rather than failing the whole transaction), enforcing a minimum order size relative to the pair's balances/quantity limit to make dust-order flooding uneconomical, or capping/aggregating matches per price level so an attacker cannot force victims to always cross more than `MAX_MATCH_NUM` distinct maker orders.

### Proof of Concept
1. Attacker creates ≥21 `MarketSellAssetContract` orders selling token A for token B, each with minimal `sellTokenQuantity` (e.g., 1) but slightly different prices, spread across one or more accounts to stay under `MAX_ACTIVE_ORDER_NUM` per account — see order book seeding pattern used in the existing test `exceedMaxMatchNumLimit`: [7](#0-6) 
2. Victim submits a legitimate `MarketSellAssetContract` selling B for A with a size that would naturally match across all of these small orders.
3. `matchOrder()` walks the order list, increments `matchOrderCount`, and once it passes 20, throws `"Too many matches. MAX_MATCH_NUM = 20"`, causing `execute()` to fail and the victim's trade transaction to revert.
4. As long as the attacker keeps ≥21 dust orders live on that pair (cheap to maintain), every victim transaction attempting to cross that many levels will keep failing, denying market access for that pair.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L62-66)
```java
  @Getter
  @Setter
  private static int MAX_ACTIVE_ORDER_NUM = 100;
  @Getter
  private static int MAX_MATCH_NUM = 20;
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L133-159)
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

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L288-291)
```java
  @Override
  public long calcFee() {
    return dynamicStore.getMarketSellFee();
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L342-359)
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
