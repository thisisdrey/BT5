## Title
Market order-matching rounding (`multiplyAndDivide` floor division) lacks split-invariance, enabling profit extraction by fragmenting a maker's inventory into many small orders before a taker sweep - (File: `actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java`)

### Summary
`MarketSellAssetActuator.matchSingleOrder()` computes the TRC10/TRX amount a maker receives using integer floor division (`MarketUtils.multiplyAndDivide`). When a taker order is large enough to fully consume a maker order ("taker > maker" branch), the taker receives the maker's *entire* remaining sell-token balance but only pays `floor(makerSellRemainQuantity * makerBuyQuantity / makerSellQuantity)` from its own sell-token balance. This floor operation discards a fractional unit of value on every matched maker order. Consolidating the same total liquidity into one maker order only discards the fraction once, but fragmenting it into N small maker orders at identical price lets a sweeping taker discard the fraction N times, extracting more net value than a single equivalent trade would allow — the same "lacks split invariance under rounding" flaw described in the USM `defund()` incident.

### Finding Description
In `matchSingleOrder()` [1](#0-0) , the "taker > maker" branch:
```
takerBuyTokenQuantityReceive = makerOrderCapsule.getSellTokenQuantityRemain();
makerBuyTokenQuantityReceive = MarketUtils
    .multiplyAndDivide(makerSellRemainQuantity, makerBuyQuantity, makerSellQuantity, ...);
...
makerOrderCapsule.setSellTokenQuantityRemain(0);
takerOrderCapsule.setSellTokenQuantityRemain(subtractExact(
    takerOrderCapsule.getSellTokenQuantityRemain(), makerBuyTokenQuantityReceive));
```
The taker receives the maker's *full* remaining sell-token quantity, but is only debited `makerBuyTokenQuantityReceive`, computed via `MarketUtils.multiplyAndDivide`, which uses `floorDiv`/`BigInteger.divide` (truncating toward zero) [2](#0-1) . Each such floor operation discards up to `1` unit of the taker's sell-token cost relative to the exact ratio.

`matchOrder()` iterates over up to `MAX_MATCH_NUM` maker orders in a single taker transaction, invoking `matchSingleOrder()` once per maker order [3](#0-2) . If the same total maker liquidity is expressed as one large order, the rounding loss/benefit occurs once; if fragmented into many small orders at the same price, the floor-division discount is applied independently to each order, so the taker's cumulative discount scales with the number of matched orders rather than with total traded volume — a direct analog to USM's per-call arithmetic-mean rounding lacking "split invariance."

### Impact Explanation
An attacker who controls (or colludes to create) many small maker orders at a chosen price, then submits one taker `MarketSellAsset` transaction that sweeps through them (bounded by `MAX_MATCH_NUM`), pays systematically less sell-token than a single consolidated trade of equal size would require. This constitutes unauthorized/unbacked extraction of TRC10/TRX value from the order-matching engine (a form of unbacked balance creation via rounding, matching the "theft or permanent freezing of funds / unbacked balance" impact criteria), scaled by the number of fragmented orders matched per transaction.

### Likelihood Explanation
Reachable by any unprivileged account through the standard `MarketSellAssetContract` / `MarketSellAssetActuator` path — no special privileges required, only the ability to place TRC10/TRX market orders (whitelisted "exchange and market order handling" surface). The attacker only needs to control both sides (or coordinate maker/taker) and pay the fixed `MarketSellFee` per order plus bandwidth/energy costs; profit scales with the number of matched fragments and token value, so it is economically viable for tokens with meaningful per-unit value.

### Recommendation
Compute `makerBuyTokenQuantityReceive` using rounding that favors the pool/maker consistently (e.g., round up when the taker is the one being credited the full maker inventory), or perform the whole-sweep settlement using a single aggregated ratio computation instead of per-maker-order floor divisions, so that the total taker cost for a given total quantity traded is invariant to how the maker-side liquidity is fragmented into multiple orders.

### Proof of Concept
1. Attacker (address A) posts N small maker sell orders for token X, each selling `sellQty` of X for `buyQty` of TRX at identical price ratio `sellQty:buyQty` chosen such that `multiplyAndDivide(sellQty, buyQty, sellQty)` truncates a fractional unit (e.g., `makerSellQuantity=3, makerBuyQuantity=1` repeated across N orders).
2. Attacker (address B, or same attacker) submits one `MarketSellAssetContract` taker order large enough to fully consume all N maker orders in a single `matchOrder()` call.
3. For each of the N matches, `matchSingleOrder()`'s "taker > maker" branch grants B the maker's full `sellTokenQuantityRemain` while debiting B only `floor(...)`, discarding up to 1 unit of TRX cost per matched order.
4. Compare total TRX debited from B against a control scenario where the same total X liquidity is posted as a single maker order of size `N*sellQty` — the single-order scenario discards at most 1 unit total, while the fragmented scenario discards up to N units, demonstrating the split-dependent rounding advantage, verifiable against the existing test scaffolding in `MarketSellAssetActuatorTest` (e.g. `partMatchMakerBuyOrders1`/`partMatchMakerLeftNotEnoughBuyOrders1`) [4](#0-3) .

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L307-380)
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

      // the orders of makerPrice have been all consumed
      if (orderIdListCapsule.isOrderEmpty()) {
        pairPriceToOrderStore.delete(pairPriceKey);

        // need to delete marketPair if no more price(priceKeysList is empty after deleting)
        priceKeysList.remove(0);

        // update priceInfo's count
        remainCount = remainCount - 1;
        // if really empty, need to delete token pair from pairToPriceStore
        if (remainCount == 0) {
          pairToPriceStore.delete(makerPair);
          break;
        } else {
          pairToPriceStore.setPriceNum(makerPair, remainCount);
        }
      }
    } // end while
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L453-482)
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
```

**File:** chainbase/src/main/java/org/tron/core/capsule/utils/MarketUtils.java (L264-277)
```java
  public static long multiplyAndDivide(long a, long b, long c, boolean disableMath) {
    try {
      long tmp = multiplyExact(a, b, disableMath);
      return floorDiv(tmp, c, disableMath);
    } catch (ArithmeticException ex) {
      // do nothing here, because we will use BigInteger to compute again
    }

    BigInteger aBig = BigInteger.valueOf(a);
    BigInteger bBig = BigInteger.valueOf(b);
    BigInteger cBig = BigInteger.valueOf(c);

    return aBig.multiply(bBig).divide(cBig).longValue();
  }
```

**File:** framework/src/test/java/org/tron/core/actuator/MarketSellAssetActuatorTest.java (L1445-1591)
```java
  /**
   * match with 2 existing buy orders and complete the makers
   */
  @Test
  public void partMatchMakerBuyOrders1() throws Exception {

    InitAsset();

    //(sell id_1  and buy id_2)
    String sellTokenId = TOKEN_ID_ONE;
    long sellTokenQuant = 800L;
    String buyTokenId = TOKEN_ID_TWO;
    long buyTokenQuant = 200L;

    byte[] ownerAddress = ByteArray.fromHexString(OWNER_ADDRESS_FIRST);
    AccountCapsule accountCapsule = dbManager.getAccountStore().get(ownerAddress);
    accountCapsule.addAssetAmountV2(sellTokenId.getBytes(), sellTokenQuant,
        dbManager.getDynamicPropertiesStore(), dbManager.getAssetIssueStore());
    dbManager.getAccountStore().put(ownerAddress, accountCapsule);
    Assert.assertEquals(sellTokenQuant,
            (long) accountCapsule.getAssetV2MapForTest().get(sellTokenId));

    //get storeDB instance
    ChainBaseManager chainBaseManager = dbManager.getChainBaseManager();
    MarketAccountStore marketAccountStore = chainBaseManager.getMarketAccountStore();
    MarketOrderStore orderStore = chainBaseManager.getMarketOrderStore();
    MarketPairToPriceStore pairToPriceStore = chainBaseManager.getMarketPairToPriceStore();
    MarketPairPriceToOrderStore pairPriceToOrderStore = chainBaseManager
        .getMarketPairPriceToOrderStore();

    // Initialize the order book

    //add three order(sell id_2 and buy id_1) with different price by the same account
    //TOKEN_ID_TWO is twice as expensive as TOKEN_ID_ONE
    addOrder(TOKEN_ID_TWO, 100L, TOKEN_ID_ONE,
        200L, OWNER_ADDRESS_SECOND);
    addOrder(TOKEN_ID_TWO, 100L, TOKEN_ID_ONE,
        300L, OWNER_ADDRESS_SECOND);
    addOrder(TOKEN_ID_TWO, 100L, TOKEN_ID_ONE,
        500L, OWNER_ADDRESS_SECOND);

    Assert.assertEquals(3,
        pairToPriceStore.getPriceNum(TOKEN_ID_TWO.getBytes(), TOKEN_ID_ONE.getBytes()));

    // do process
    MarketSellAssetActuator actuator = new MarketSellAssetActuator();
    actuator.setChainBaseManager(dbManager.getChainBaseManager()).setAny(getContract(
        OWNER_ADDRESS_FIRST, sellTokenId, sellTokenQuant, buyTokenId, buyTokenQuant));

    TransactionResultCapsule ret = new TransactionResultCapsule();
    actuator.validate();
    actuator.execute(ret);

    //check balance and token
    accountCapsule = dbManager.getAccountStore().get(ownerAddress);
    Assert.assertEquals(0L,
            (long) accountCapsule.getAssetV2MapForTest().get(sellTokenId));
    Assert.assertEquals(200L,
            (long) accountCapsule.getAssetV2MapForTest().get(buyTokenId));

    byte[] makerAddress = ByteArray.fromHexString(OWNER_ADDRESS_SECOND);
    AccountCapsule makerAccountCapsule = dbManager.getAccountStore().get(makerAddress);
    Assert.assertEquals(500L,
            (long) makerAccountCapsule.getAssetV2MapForTest().get(sellTokenId));

    //check accountOrder
    MarketAccountOrderCapsule accountOrderCapsule = marketAccountStore.get(ownerAddress);
    Assert.assertEquals(1, accountOrderCapsule.getCount());
    ByteString orderId = accountOrderCapsule.getOrdersList().get(0);

    MarketAccountOrderCapsule makerAccountOrderCapsule = marketAccountStore.get(makerAddress);
    Assert.assertEquals(1, makerAccountOrderCapsule.getCount());
    // ByteString makerOrderId1 = makerAccountOrderCapsule.getOrdersList().get(0);
    // ByteString makerOrderId2 = makerAccountOrderCapsule.getOrdersList().get(1);

    //check order
    MarketOrderCapsule orderCapsule = orderStore.get(orderId.toByteArray());
    Assert.assertEquals(300L, orderCapsule.getSellTokenQuantityRemain());
    Assert.assertEquals(State.ACTIVE, orderCapsule.getSt());

    // MarketOrderCapsule makerOrderCapsule1 = orderStore.get(makerOrderId1.toByteArray());
    // Assert.assertEquals(0L, makerOrderCapsule1.getSellTokenQuantityRemain());
    // Assert.assertEquals(State.INACTIVE, makerOrderCapsule1.getSt());

    // MarketOrderCapsule makerOrderCapsule2 = orderStore.get(makerOrderId2.toByteArray());
    // Assert.assertEquals(0L, makerOrderCapsule2.getSellTokenQuantityRemain());
    // Assert.assertEquals(State.INACTIVE, makerOrderCapsule2.getSt());

    //check pairToPrice
    Assert.assertEquals(1,
        pairToPriceStore.getPriceNum(sellTokenId.getBytes(), buyTokenId.getBytes()));

    List<byte[]> takerPriceKeysList = pairPriceToOrderStore
        .getPriceKeysList(sellTokenId.getBytes(), buyTokenId.getBytes(), 1);
    MarketPrice takerPrice = MarketUtils.decodeKeyToMarketPrice(takerPriceKeysList.get(0));
    // 800:200 => 4:1
    Assert.assertEquals(4L, takerPrice.getSellTokenQuantity());
    Assert.assertEquals(1L, takerPrice.getBuyTokenQuantity());

    Assert.assertEquals(1,
        pairToPriceStore.getPriceNum(TOKEN_ID_TWO.getBytes(), TOKEN_ID_ONE.getBytes()));

    List<byte[]> makerPriceKeysList = pairPriceToOrderStore
        .getPriceKeysList(TOKEN_ID_TWO.getBytes(), TOKEN_ID_ONE.getBytes(), 1);
    MarketPrice marketPrice = MarketUtils.decodeKeyToMarketPrice(makerPriceKeysList.get(0));
    // 100:500 => 1:5
    Assert.assertEquals(1L, marketPrice.getSellTokenQuantity());
    Assert.assertEquals(5L, marketPrice.getBuyTokenQuantity());

    //check pairPriceToOrder
    byte[] pairPriceKey = MarketUtils.createPairPriceKey(
        TOKEN_ID_TWO.getBytes(), TOKEN_ID_ONE.getBytes(),
        100L, 200L);
    MarketOrderIdListCapsule orderIdListCapsule = pairPriceToOrderStore
        .getUnchecked(pairPriceKey);
    Assert.assertNull(orderIdListCapsule);

    pairPriceKey = MarketUtils.createPairPriceKey(
        TOKEN_ID_TWO.getBytes(), TOKEN_ID_ONE.getBytes(),
        100L, 300L);
    orderIdListCapsule = pairPriceToOrderStore
        .getUnchecked(pairPriceKey);
    Assert.assertNull(orderIdListCapsule);

    Assert.assertEquals(2, ret.getOrderDetailsList().size());

    MarketOrderDetail orderDetail = ret.getOrderDetailsList().get(0);
    // Assert.assertEquals(makerOrderCapsule1.getID(), orderDetail.getMakerOrderId());
    Assert.assertEquals(orderCapsule.getID(), orderDetail.getTakerOrderId());
    Assert.assertEquals(200L, orderDetail.getFillSellQuantity());
    Assert.assertEquals(100L, orderDetail.getFillBuyQuantity());
    MarketOrderCapsule makerOrderCapsule1 = orderStore
        .get(orderDetail.getMakerOrderId().toByteArray());
    Assert.assertEquals(0L, makerOrderCapsule1.getSellTokenQuantityRemain());
    Assert.assertEquals(State.INACTIVE, makerOrderCapsule1.getSt());

    orderDetail = ret.getOrderDetailsList().get(1);
    // Assert.assertEquals(makerOrderCapsule2.getID(), orderDetail.getMakerOrderId());
    Assert.assertEquals(orderCapsule.getID(), orderDetail.getTakerOrderId());
    Assert.assertEquals(300L, orderDetail.getFillSellQuantity());
    Assert.assertEquals(100L, orderDetail.getFillBuyQuantity());
    MarketOrderCapsule makerOrderCapsule2 = orderStore
        .get(orderDetail.getMakerOrderId().toByteArray());
    Assert.assertEquals(0L, makerOrderCapsule2.getSellTokenQuantityRemain());
    Assert.assertEquals(State.INACTIVE, makerOrderCapsule2.getSt());

  }
```
