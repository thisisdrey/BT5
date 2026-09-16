### Title
Live-read of committee-adjustable market fees causes users to pay unexpected, outdated cancel/sell fees - (File: actuator/src/main/java/org/tron/core/actuator/MarketCancelOrderActuator.java)

### Summary
`MarketCancelOrderActuator` (and equivalently `MarketSellAssetActuator`) charges a fee that is read live from `DynamicPropertiesStore` at the moment of `validate()`/`execute()`, rather than a value captured or agreed upon when the order was created. Since `MARKET_CANCEL_FEE`/`MARKET_SELL_FEE` are chain parameters that the committee can change at any time via proposal, a user who places an order expecting to pay the fee in effect at creation time can end up paying a materially different (higher) fee when they cancel, exactly mirroring the SecondSwap "outdated penalty fee" bug class.

### Finding Description
When a user places a sell order via `MarketSellAssetActuator`, no fee is stored on the order itself — only the order's token/price data is persisted in `MarketOrderCapsule`. [1](#0-0) 

Later, when the user (or the s2Admin-equivalent) cancels the order via `MarketCancelOrderActuator.execute()`, the fee charged is computed by `calcFee()`, which simply reads the current value of `MARKET_CANCEL_FEE` from `DynamicPropertiesStore`: [2](#0-1) 

Both `validate()` and `execute()` call `calcFee()` independently and at the time the transaction is processed, not at the time the order was listed: [3](#0-2) [4](#0-3) 

`MARKET_CANCEL_FEE` (and `MARKET_SELL_FEE`) is a standard committee-governed dynamic parameter, changeable at any time by a passed proposal, with no timelock tied to existing open orders: [5](#0-4) [6](#0-5) 

This is structurally identical to the SecondSwap bug: an outdated/expected fee cached nowhere is silently replaced by whatever fee is active at settlement time, with no cap, cache, or user-facing protection mechanism.

### Impact Explanation
A user who submits a `MarketSellAssetContract` order expecting the current `MARKET_CANCEL_FEE` (e.g., near-zero, per test defaults) can, between order placement and cancellation, be forced to pay a substantially higher fee if the committee raises `MARKET_CANCEL_FEE` via proposal in the interim. Because the fee is directly deducted from the account balance in `execute()` (`accountCapsule.setBalance(accountCapsule.getBalance() - fee)`) and burned/sent to the blackhole, this results in unbacked, unexpected loss of TRX for the order owner relative to what they reasonably expected when they placed the order — the same "unfair fee charge" class flagged as Medium severity in the SecondSwap report. Unlike a typical TRX cost-of-doing-business change (e.g., bandwidth/energy prices, which apply uniformly to all future transactions), here the parameter retroactively affects a pre-existing, already-created financial position (the open order), which the user cannot exit without accepting the new fee.

### Likelihood Explanation
Requires the committee to pass a proposal changing `MARKET_CANCEL_FEE` (or `MARKET_SELL_FEE`) while a user has an open order — a legitimate, permitted governance action that happens independent of any specific user's order lifecycle. This is a systemic design gap rather than a rare edge case: any active order at proposal-effect time is exposed. It does not require malicious behavior by any specific actor, only normal parameter governance combined with normal user market activity, making it a realistic occurrence rather than a purely theoretical one.

### Recommendation
Snapshot the applicable fee at order-creation time inside `MarketOrderCapsule` (similar to storing `createTime`), and have `MarketCancelOrderActuator.calcFee()` read the cached per-order fee instead of the live `DynamicPropertiesStore` value. Alternatively, apply proposal-driven fee changes only to orders created after the parameter change takes effect, or provide a grace/notice period during which existing open orders are unaffected.

### Proof of Concept
1. User calls `MarketSellAssetActuator` to place a sell order while `MARKET_SELL_FEE`/`MARKET_CANCEL_FEE` = X (paid at creation per `calcFee()` in `MarketSellAssetActuator.java:288-291`).
2. Before the user cancels, the committee passes a proposal that raises `MARKET_CANCEL_FEE` to Y (Y >> X) via `ProposalService` (`case MARKET_CANCEL_FEE: manager.getDynamicPropertiesStore().saveMarketCancelFee(entry.getValue());`).
3. User calls `MarketCancelOrderActuator.unlistVesting`-equivalent (`execute`) to cancel their still-open order.
4. `calcFee()` in `MarketCancelOrderActuator.java:227-229` reads the now-updated `MARKET_CANCEL_FEE` = Y, and `execute()` deducts Y (not X) from the user's balance (`MarketCancelOrderActuator.java:96-102`), charging the user an outdated/unexpected fee they never agreed to when the order was placed.

### Citations

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

**File:** actuator/src/main/java/org/tron/core/actuator/MarketCancelOrderActuator.java (L84-102)
```java
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
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketCancelOrderActuator.java (L212-217)
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

**File:** framework/src/main/java/org/tron/core/consensus/ProposalService.java (L228-235)
```java
        case MARKET_SELL_FEE: {
          manager.getDynamicPropertiesStore().saveMarketSellFee(entry.getValue());
          break;
        }
        case MARKET_CANCEL_FEE: {
          manager.getDynamicPropertiesStore().saveMarketCancelFee(entry.getValue());
          break;
        }
```

**File:** chainbase/src/main/java/org/tron/core/store/DynamicPropertiesStore.java (L1674-1685)
```java
  public void saveMarketCancelFee(long fee) {
    this.put(MARKET_CANCEL_FEE,
        new BytesCapsule(ByteArray.fromLong(fee)));
  }

  public long getMarketCancelFee() {
    return Optional.ofNullable(getUnchecked(MARKET_CANCEL_FEE))
        .map(BytesCapsule::getData)
        .map(ByteArray::toLong)
        .orElseThrow(
            () -> new IllegalArgumentException("not found MARKET_CANCEL_FEE"));
  }
```
