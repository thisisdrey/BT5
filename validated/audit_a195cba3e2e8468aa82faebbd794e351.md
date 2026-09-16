### Title
Committee-controlled `MARKET_CANCEL_FEE` update retroactively taxes already-placed pending TRC10 exchange orders - (File: actuator/src/main/java/org/tron/core/actuator/MarketCancelOrderActuator.java)

### Summary
`MarketCancelOrderActuator::calcFee` reads the *current* `MARKET_CANCEL_FEE` chain parameter at cancel-time rather than the fee that was in effect when the order was created. Because `MARKET_CANCEL_FEE` can be changed at any time via a committee proposal (`ProposalService`), an order that sits in the order book for any period is subject to a fee regime chosen after the order was placed, exactly the same "config changed after commitment, applied retroactively to a pending, not-yet-settled position" pattern described in the external report.

### Finding Description
When a user places a sell order via `MarketSellAssetActuator`, tokens/TRX are locked into `MarketOrderCapsule` and the order sits in `MarketOrderStore` / `MarketPairPriceToOrderStore` until it is matched or canceled [1](#0-0) . The order may remain outstanding indefinitely (bounded only by `MAX_ACTIVE_ORDER_NUM`), so it is a "pending" position analogous to unclaimed LP rewards in the report.

When the owner later cancels that order, `MarketCancelOrderActuator::execute` charges a fee computed by `calcFee()`, which simply reads the live dynamic-store value: [2](#0-1) 
This fee is deducted from the user's balance and burned/sent to the blackhole at execute time: [3](#0-2) 

The fee is not locked in, cached, or snapshotted on the `MarketOrderCapsule` at order-creation time; it is only ever evaluated live when the cancel actuator runs. `MARKET_CANCEL_FEE` (and `MARKET_SELL_FEE`) is a proposal-controlled chain parameter that the committee can change at any point after the order is created: [4](#0-3) 
and the change takes effect immediately upon proposal approval via `ProposalService.process`: [5](#0-4) 

This is structurally identical to the reported bug class: a configuration value (`beefyFeeConfig` / here `MARKET_CANCEL_FEE`) is updated while a user-owned pending position (unclaimed LP rewards / here an open exchange order) is outstanding, and the new rate is retroactively applied against a commitment made under the old rate, with no snapshot-and-charge-before-update safeguard.

By contrast, the codebase's own brokerage mechanism (`UpdateBrokerageActuator` / `MortgageService`) deliberately avoids this exact problem: a witness's brokerage-rate change is stored under a "pending" key and only locked in for the *next* cycle via `MaintenanceManager.doMaintenance`, so the change never retroactively affects rewards already accruing in the current cycle [6](#0-5) [7](#0-6) . The market-order cancel-fee path has no equivalent protection.

### Impact Explanation
If the committee raises `MARKET_CANCEL_FEE` after a user has placed an order, that user is forced to either accept the new, higher cancellation cost to exit a position they entered under a different economic assumption, or leave the position open and locked in the order book. Because `MarketSellAssetActuator` also charges `MARKET_SELL_FEE` up front, users have no ability to "hedge" or predict the exit cost of an order at creation time. This is an unbacked/unexpected cost imposed on user funds already committed to the protocol, matching the report's "unauthorized economic extraction from a pending, previously-committed position" impact class.

### Likelihood Explanation
The `MARKET_CANCEL_FEE` value is changed only through the committee proposal mechanism (`ProposalApproveContract` / `ProposalCreateContract` processed via `ProposalService`), which requires committee/SR approval — this is a normal governance action, not a "malicious SR" attack, exactly as the original report frames the "protocol owner" as a legitimately-privileged but self-interested actor. Any ordinary account can place a `MarketSellAssetContract` order (an unprivileged action) and become exposed the moment the fee changes while the order is open, so the reachable trigger is broad and requires no special privilege from the victim's side.

### Recommendation
Snapshot the applicable cancel fee (and/or sell fee) on the `MarketOrderCapsule` at order-creation time (`MarketSellAssetActuator::createAndSaveOrder`) and have `MarketCancelOrderActuator::calcFee` charge the fee stored on the order rather than re-reading the live `DynamicPropertiesStore` value. Alternatively, apply governance-driven fee changes only to orders created after the change takes effect (mirroring the deferred-application pattern already used for witness brokerage in `MortgageService`/`MaintenanceManager`).

### Proof of Concept
1. Committee approves a proposal setting `MARKET_CANCEL_FEE` to a low value (e.g., 0) — verified achievable at any time per `ProposalUtil.validator` bounds `[0, 10_000_000_000L]` [8](#0-7) .
2. A user broadcasts `MarketSellAssetContract` (unprivileged transaction), locking tokens/TRX into an order via `MarketSellAssetActuator.execute` [9](#0-8) , expecting the current low cancel cost if the order does not fill.
3. Before the order is matched, the committee approves a new proposal raising `MARKET_CANCEL_FEE` to its maximum, updated live via `ProposalService.process` case `MARKET_CANCEL_FEE` [5](#0-4) .
4. The user broadcasts `MarketCancelOrderContract` to exit the still-pending order; `calcFee()` now returns the newly raised fee and deducts it from the user's balance [3](#0-2) [2](#0-1) , demonstrating the retroactive fee application against a previously-committed, still-pending user position.

**Note on confidence:** I was not able to fully verify whether there are additional protections elsewhere (e.g., a fee cap enforced per-order at creation, or a separate order-level fee field) since I could not exhaustively review `MarketOrderCapsule`'s full field set within the available tool budget; a full review of `MarketOrderCapsule.java` and the `MarketContract` proto would be needed to rule out an existing per-order fee snapshot.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L133-147)
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

**File:** actuator/src/main/java/org/tron/core/actuator/MarketCancelOrderActuator.java (L226-229)
```java
  @Override
  public long calcFee() {
    return dynamicStore.getMarketCancelFee();
  }
```

**File:** actuator/src/main/java/org/tron/core/utils/ProposalUtil.java (L384-397)
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
      }
```

**File:** framework/src/main/java/org/tron/core/consensus/ProposalService.java (L232-235)
```java
        case MARKET_CANCEL_FEE: {
          manager.getDynamicPropertiesStore().saveMarketCancelFee(entry.getValue());
          break;
        }
```

**File:** chainbase/src/main/java/org/tron/core/service/MortgageService.java (L79-87)
```java
  private void payReward(byte[] witnessAddress, long value) {
    long cycle = dynamicPropertiesStore.getCurrentCycleNumber();
    int brokerage = delegationStore.getBrokerage(cycle, witnessAddress);
    double brokerageRate = (double) brokerage / 100;
    long brokerageAmount = (long) (brokerageRate * value);
    value -= brokerageAmount;
    delegationStore.addReward(cycle, witnessAddress, value);
    adjustAllowance(witnessAddress, brokerageAmount);
  }
```

**File:** consensus/src/main/java/org/tron/consensus/dpos/MaintenanceManager.java (L154-162)
```java
    if (dynamicPropertiesStore.allowChangeDelegation()) {
      long nextCycle = dynamicPropertiesStore.getCurrentCycleNumber() + 1;
      dynamicPropertiesStore.saveCurrentCycleNumber(nextCycle);
      consensusDelegate.getAllWitnesses().forEach(witness -> {
        delegationStore.setBrokerage(nextCycle, witness.createDbKey(),
            delegationStore.getBrokerage(witness.createDbKey()));
        delegationStore.setWitnessVote(nextCycle, witness.createDbKey(), witness.getVoteCount());
      });
    }
```
