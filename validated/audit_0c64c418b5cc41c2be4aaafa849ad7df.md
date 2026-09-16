This confirms the analog. The `MarketCancelOrderContract` mechanics mirror the Putty bug precisely: an order is created and persists indefinitely in the order book, but the fee charged when the order owner later cancels it is computed dynamically at cancel-time via `calcFee()` → `dynamicStore.getMarketCancelFee()`, rather than being fixed/snapshotted into the `MarketOrderCapsule` at creation time.

### Title
Market order cancellation fee is read from live governance state instead of being locked at order creation, letting later fee hikes tax users without consent - (File: actuator/src/main/java/org/tron/core/actuator/MarketCancelOrderActuator.java)

### Summary
`MarketSellAssetActuator` lets any account place a resting sell order (`MarketOrderCapsule`) that can sit in the order book indefinitely [1](#0-0) . The only fee charged when this order is created is `MARKET_SELL_FEE`, deducted atomically in the same transaction [2](#0-1) . Nowhere is a cancellation fee stored on the order itself. When the owner later cancels that order via `MarketCancelOrderActuator`, `calcFee()` re-reads the *current* `MARKET_CANCEL_FEE` dynamic parameter and deducts it from the owner's balance at cancellation time [3](#0-2) , with the balance check performed the same way in `validate()` [4](#0-3) .

### Finding Description
`MARKET_CANCEL_FEE` is a committee-governed dynamic property that can be changed at any time via a standard proposal, independent of any specific order. `ProposalUtil` allows the value to be set anywhere in the range `[0, 10_000_000_000]` sun (up to 10,000 TRX) [5](#0-4) , and once approved, `ProposalService.process` immediately persists the new value via `saveMarketCancelFee` [6](#0-5) .

Because `MarketOrderCapsule` never records the fee (or a fee "at creation" snapshot) that applied when the order was placed, and `MarketCancelOrderActuator.calcFee()` simply reads `dynamicStore.getMarketCancelFee()` at execution time, an order placed when the cancel fee was low (or 0) can later be subject to a much higher fee purely because the network-wide parameter changed in the interim — exactly the class of bug described in the Putty finding, where the fee applicable to a user's position is not fixed at the time the user commits funds, but is instead evaluated later against mutable global state.

### Impact Explanation
A user who places a sell order with the intent to be able to cancel it cheaply (or freely, if the fee was 0 at the time) has no guarantee that this remains true. If the committee raises `MARKET_CANCEL_FEE` while the order is still resting in the order book:
- The user is forced to pay an unexpected, unconsented-to fee to reclaim their listed tokens/TRX, directly reducing their expected withdrawal — a loss of funds analogous to the original report.
- If the user's balance is insufficient to cover the new, higher fee, `validate()` rejects the cancellation with "No enough balance!" [4](#0-3) , permanently freezing the order's escrowed tokens in the market store until/unless the user tops up their account balance — a form of unbacked/locked funds enforced entirely by a parameter the user never agreed to when creating the order.

### Likelihood Explanation
Any account can create a resting order via a normal `MarketSellAssetContract` transaction, and fee-parameter changes occur through the routine, sanctioned committee proposal process (not a malicious actor exploiting a bug) — so this is trivially reachable in production operation, and the resulting loss/freezing of funds requires no special privilege from the affected user, only the passage of time between order placement and a legitimate governance vote.

### Recommendation
Snapshot the fee applicable to cancellation (and/or sell) into the `MarketOrderCapsule` at order-creation time, and use that stored value in `MarketCancelOrderActuator.calcFee()` instead of re-reading the live `DynamicPropertiesStore` value. Alternatively, cap/limit how much `MARKET_CANCEL_FEE` may increase relative to the value in effect when outstanding orders were created, or grandfather existing orders under the fee that was active at their creation time.

### Proof of Concept
1. Committee sets `MARKET_CANCEL_FEE = 0` (or a low value) via proposal.
2. Alice submits `MarketSellAssetContract`, creating a resting sell order (`MarketOrderCapsule`) that is only partially matched and remains in `MarketOrderStore`/`MarketPairPriceToOrderStore` [7](#0-6) .
3. The committee later approves a proposal raising `MARKET_CANCEL_FEE` to `10_000_000_000` sun (10,000 TRX), immediately effective via `ProposalService` [6](#0-5) .
4. Alice submits `MarketCancelOrderContract` to cancel her still-resting order. `calcFee()` now returns the new 10,000 TRX fee, which is deducted from her balance [8](#0-7) , or, if her balance is insufficient, the cancellation reverts and her escrowed tokens remain locked indefinitely.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L108-132)
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
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L136-147)
```java
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

**File:** actuator/src/main/java/org/tron/core/actuator/MarketCancelOrderActuator.java (L90-102)
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
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketCancelOrderActuator.java (L212-216)
```java
    // Whether the balance is enough
    long fee = calcFee();
    if (ownerAccount.getBalance() < fee) {
      throw new ContractValidateException("No enough balance !");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketCancelOrderActuator.java (L226-229)
```java
  @Override
  public long calcFee() {
    return dynamicStore.getMarketCancelFee();
  }
```

**File:** actuator/src/main/java/org/tron/core/utils/ProposalUtil.java (L384-396)
```java
      case MARKET_CANCEL_FEE: {
        if (!forkController.pass(ForkBlockVersionEnum.VERSION_4_1)) {
          throw new ContractValidateException("Bad chain parameter id [MARKET_CANCEL_FEE]");
        }
        if (!dynamicPropertiesStore.supportAllowMarketTransaction()) {
          throw new ContractValidateException(
              "Market Transaction is not activated, can not set Market Cancel Fee");
        }
        if (value < 0 || value > 10_000_000_000L) {
          throw new ContractValidateException(
              "Bad MARKET_CANCEL_FEE parameter value, valid range is [0,10_000_000_000L]");
        }
        break;
```

**File:** framework/src/main/java/org/tron/core/consensus/ProposalService.java (L232-235)
```java
        case MARKET_CANCEL_FEE: {
          manager.getDynamicPropertiesStore().saveMarketCancelFee(entry.getValue());
          break;
        }
```
