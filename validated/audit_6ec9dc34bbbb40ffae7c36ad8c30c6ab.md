### Title
No expiration/deadline in TRC10 `MarketSellAssetContract` limit orders allows stale orders to be filled under changed market conditions - (File: `protocol/src/main/protos/core/contract/market_contract.proto`)

### Summary
Like the reported Teller `createCommitment` issue, java-tron's on-chain TRC10 exchange order book lets any account create a persistent limit order (`MarketSellAssetContract`) with no expiration/deadline. The order stays `ACTIVE` in the order book indefinitely and can be matched (filled) by any other unprivileged account's `MarketSellAssetContract` transaction at any point in the future, regardless of how much time has passed or how market conditions have changed. The only way to remove it is an explicit `MarketCancelOrderContract` from the owner.

### Finding Description
The `MarketOrder` protobuf message (`protocol/src/main/protos/core/Tron.proto:62-84`) stores `create_time` but has no expiration/deadline field: [1](#0-0) 

The `MarketSellAssetContract` that clients submit to place an order likewise carries no deadline parameter: [2](#0-1) 

`MarketSellAssetActuator.doValidate()` performs address/balance/asset-existence/quantity checks but never validates any time-based constraint on the order, and there is no other authorization needed to submit an order or to trigger a match against a pre-existing maker order: [3](#0-2) 

When an order is only partially filled, the remainder is persisted back into the order book via `saveRemainOrder`, with `createAndSaveOrder` recording only `create_time` (no expiry) before the order sits in `MarketPairPriceToOrderStore`/`MarketOrderStore` until fully matched or explicitly canceled: [4](#0-3) 

`matchOrder`/`matchSingleOrder`, invoked by any subsequent taker's `MarketSellAssetContract`, matches purely on price compatibility (`MarketUtils.priceMatch`) with no check on order age or any deadline: [5](#0-4) 

The only mechanism to remove a stale order is the maker's own explicit `MarketCancelOrderContract` via `MarketCancelOrderActuator`: [6](#0-5) 

This is functionally the same root cause as the reported issue: a persisted, unprivileged-reachable financial commitment (a resting limit order) with no self-expiring deadline, meaning a maker who forgets about the order, is griefed out of canceling it, or simply leaves it resting through a long period of price volatility can have it filled by any other account long after the maker would have wanted it active.

### Impact Explanation
A maker's asset/TRX allocation is locked into the order (see `transferBalanceOrToken`, which debits the seller's balance/asset at order placement time) for an unbounded duration. Because there's no deadline, if the relative value of `sellTokenId`/`buyTokenId` shifts substantially after order placement (which is expected for volatile TRC10 tokens), a taker can execute the stale order at the maker's original, now unfavorable, fixed exchange ratio — the exact "unfavorable deal because of volatile market conditions" impact described in the source report. Because TRON's exchange is at the base-layer consensus (not an off-chain intent system with settlement guards), there is no possibility for the maker to add off-chain slippage/deadline protection; the only on-chain defense is proactively cancelling, which is a manual, easily-forgotten step exactly as flagged in the report.

### Likelihood Explanation
Likelihood is high: placing a `MarketSellAssetContract` order and leaving it unfilled is a completely normal, expected usage pattern (users routinely leave limit orders resting to be filled later), and any account can trigger a match against a stale order simply by submitting a compatible `MarketSellAssetContract`. No special privilege is required on either side.

### Recommendation
Add an optional `expiration` (or `deadline`) field to `MarketSellAssetContract`/`MarketOrder`. In `MarketSellAssetActuator.doValidate()`/`matchSingleOrder`, reject matching against (or auto-cancel/return funds for) orders whose expiration has passed, using `dynamicStore.getLatestBlockHeaderTimestamp()` as the reference clock (consistent with how `create_time` is already set). Allow `0`/unset to mean "no expiration," matching the recommendation in the source report, to preserve backward compatibility for the many existing orders lacking this field.

### Proof of Concept
1. Account A submits `MarketSellAssetContract{sellTokenId: X, sellTokenQuantity: 100, buyTokenId: Y, buyTokenQuantity: 200}` when 100 X ≈ 200 Y in market value. `MarketSellAssetActuator.execute` debits A's X balance and, if unmatched, calls `saveRemainOrder` to persist the order indefinitely (`actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java:99-163, 501-525`).
2. Time passes (weeks/months); the market price of X relative to Y moves substantially so that 100 X is now worth far more than 200 Y. A never cancels the order (forgets, or is unable to submit `MarketCancelOrderContract` for any reason).
3. Any unrelated account B submits `MarketSellAssetContract{sellTokenId: Y, sellTokenQuantity: 200, buyTokenId: X, buyTokenQuantity: 100}`. `matchOrder`/`matchSingleOrder` match purely by price ratio with no time check (`actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java:296-380`), and the trade executes at A's stale price, transferring A's 100 X to B for only 200 Y — a deal A would not have agreed to at current market value.

**Uncertainty note:** I was unable to fully verify whether any TIP/config flag (e.g. `dynamicStore.supportAllowMarketTransaction()`) or a newer contract version elsewhere in the repo already introduces an expiration mechanism for `MarketOrder`; based on the proto definitions and actuator logic reviewed, no such field or check exists in this codebase snapshot.

### Citations

**File:** protocol/src/main/protos/core/Tron.proto (L62-84)
```text
message MarketOrder {
    bytes order_id = 1;
    bytes owner_address = 2;
    int64 create_time = 3;
    bytes sell_token_id = 4;
    int64 sell_token_quantity = 5;
    bytes buy_token_id = 6;
    int64 buy_token_quantity = 7; // min to receive
    int64 sell_token_quantity_remain = 9;
    // When state != ACTIVE and sell_token_quantity_return !=0,
    //it means that some sell tokens are returned to the account due to insufficient remaining amount
    int64 sell_token_quantity_return = 10;

    enum State {
      ACTIVE = 0;
      INACTIVE = 1;
      CANCELED = 2;
    }
    State state = 11;

    bytes prev = 12;
    bytes next = 13;
}
```

**File:** protocol/src/main/protos/core/contract/market_contract.proto (L8-14)
```text
message MarketSellAssetContract {
    bytes owner_address = 1;
    bytes sell_token_id = 2;
    int64 sell_token_quantity = 3;
    bytes buy_token_id = 4;
    int64 buy_token_quantity = 5; // min to receive
}
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L164-230)
```java
  @Override
  public boolean validate() throws ContractValidateException {
    if (this.any == null) {
      throw new ContractValidateException(CONTRACT_NOT_EXIST);
    }
    if (chainBaseManager == null) {
      throw new ContractValidateException(STORE_NOT_EXIST);
    }

    initStores();

    if (!this.any.is(MarketSellAssetContract.class)) {
      throw new ContractValidateException(
          "contract type error,expected type [MarketSellAssetContract],real type[" + any
              .getClass() + "]");
    }

    if (!dynamicStore.supportAllowMarketTransaction()) {
      throw new ContractValidateException("Not support Market Transaction, need to be opened by"
          + " the committee");
    }

    final MarketSellAssetContract contract;
    try {
      contract =
          this.any.unpack(MarketSellAssetContract.class);
    } catch (InvalidProtocolBufferException e) {
      logger.debug(e.getMessage(), e);
      throw new ContractValidateException(e.getMessage());
    }

    // Parameters check
    byte[] ownerAddress = contract.getOwnerAddress().toByteArray();
    sellTokenID = contract.getSellTokenId().toByteArray();
    buyTokenID = contract.getBuyTokenId().toByteArray();
    sellTokenQuantity = contract.getSellTokenQuantity();
    buyTokenQuantity = contract.getBuyTokenQuantity();

    if (!DecodeUtil.addressValid(ownerAddress)) {
      throw new ContractValidateException("Invalid address");
    }

    // Whether the accountStore exist
    AccountCapsule ownerAccount = accountStore.get(ownerAddress);
    if (ownerAccount == null) {
      throw new ContractValidateException("Account does not exist!");
    }

    if (!Arrays.equals(sellTokenID, "_".getBytes()) && !isNumber(sellTokenID)) {
      throw new ContractValidateException("sellTokenId is not a valid number");
    }
    if (!Arrays.equals(buyTokenID, "_".getBytes()) && !isNumber(buyTokenID)) {
      throw new ContractValidateException("buyTokenId is not a valid number");
    }

    if (Arrays.equals(sellTokenID, buyTokenID)) {
      throw new ContractValidateException("cannot exchange same tokens");
    }

    if (sellTokenQuantity <= 0 || buyTokenQuantity <= 0) {
      throw new ContractValidateException("token quantity must greater than zero");
    }

    long quantityLimit = dynamicStore.getMarketQuantityLimit();
    if (sellTokenQuantity > quantityLimit || buyTokenQuantity > quantityLimit) {
      throw new ContractValidateException("token quantity must less than " + quantityLimit);
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L296-380)
```java
  private MarketPrice hasMatch(List<byte[]> priceKeysList, MarketPrice takerPrice) {
    if (priceKeysList.isEmpty()) {
      return null;
    }

    // get the first key which is the lowest price
    MarketPrice bestPrice = MarketUtils.decodeKeyToMarketPrice(priceKeysList.get(0));

    return MarketUtils.priceMatch(takerPrice, bestPrice) ? bestPrice : null;
  }

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

**File:** actuator/src/main/java/org/tron/core/actuator/MarketCancelOrderActuator.java (L75-149)
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
  }
```
