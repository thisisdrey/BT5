### Title
Committee-controlled `ALLOW_MARKET_TRANSACTION` toggle blocks order cancellation, permanently freezing already-locked TRX/TRC10 funds - ([File: actuator/src/main/java/org/tron/core/actuator/MarketCancelOrderActuator.java])

### Summary
`MarketSellAssetActuator` and `MarketCancelOrderActuator` both gate their `validate()` on the same committee-controlled dynamic parameter, `dynamicStore.supportAllowMarketTransaction()`. Placing an order (`MarketSellAssetActuator`) locks the seller's TRX/TRC10 balance into the market order book. Cancelling that order (`MarketCancelOrderActuator`) is the *only* way for the order owner to reclaim those locked funds outside of a trade match. Because both actuators enforce the identical "market transactions must be enabled" precondition, if a committee proposal disables `ALLOW_MARKET_TRANSACTION` after a user has an open order, that user can no longer cancel the order and recover the locked funds — mirroring the reported pattern where a governance/whitelist toggle blocks the "undo"/exit path for a resource an unprivileged user already committed, while the "entry" path used the same check.

### Finding Description
`MarketSellAssetActuator.validate()` requires the feature flag to be on before allowing an order to be created and funds to be moved into the market state: [1](#0-0) 

`MarketCancelOrderActuator.validate()` enforces the exact same flag before permitting cancellation of an existing, already-placed order: [2](#0-1) 

The order's locked balance is only returned to the owner inside `execute()` of the cancel path via `MarketUtils.returnSellTokenRemain(...)`: [3](#0-2) 

There is no other unprivileged transaction path that returns the seller's locked balance for an open order except cancellation (a match via `MarketSellAssetActuator` on the opposite side, which is not controllable by the trapped user, or waiting indefinitely for a counterparty). `supportAllowMarketTransaction()` corresponds to the committee proposal parameter `ALLOW_MARKET_TRANSACTION`, settable via a standard `ProposalCreateContract`/`ProposalApproveContract` flow processed in `ProposalUtil`/`ProposalService`, i.e., it is a governance action, not something the order owner controls. Once the committee sets this flag to `0` (e.g., to pause the market for a security incident or upgrade), every account that already has a resting order in the order book loses the ability to cancel that order and recover its locked TRX or TRC10 tokens, even though the funds are sitting in `MarketAccountStore`/`MarketOrderStore` and rightfully belong to the account.

This is structurally identical to the report's core class of bug: an entry/create action and its corresponding exit/undo action for the same locked position share the same permission gate, so disabling the gate (whether via token whitelist removal or, here, a market pause) locks users out of unwinding a position they already opened, while offering no exemption for existing positions.

### Impact Explanation
Any account with a resting (unfilled or partially filled) order in the TRC10 market has its locked TRX and/or TRC10 balance become inaccessible for the duration that `ALLOW_MARKET_TRANSACTION` is disabled by the committee. This is a temporary, but committee-controlled and indefinite-duration, freezing of legitimately owned user funds with no unprivileged remedy — matching the "permanent freezing of funds" criterion, since the affected user has no transaction they can broadcast to recover the funds while the flag remains off, and re-enabling the flag is entirely outside their control.

### Likelihood Explanation
Reaching this state requires: (1) a user to place a market order via `MarketSellAssetContract` (a standard unprivileged, broadcastable transaction), and (2) the TRON committee to later disable `ALLOW_MARKET_TRANSACTION` via the existing committee proposal mechanism. The second step is a legitimate, already-implemented governance action (used, e.g., to pause the market for maintenance or in response to an incident) and is not attacker-controlled, so this is a design/consistency flaw rather than an attacker-triggerable exploit; it manifests whenever governance exercises the existing pause switch while orders are outstanding.

### Recommendation
Remove the `supportAllowMarketTransaction()` gate from `MarketCancelOrderActuator.validate()` (or otherwise special-case cancellation), so that pausing new market activity via governance does not prevent existing order owners from cancelling and reclaiming their already-locked funds. This mirrors the referenced fix pattern: the exit/repay/cancel path should not depend on the same whitelist/feature flag as the entry/deposit/order-creation path for a resource a user has already committed.

### Proof of Concept
1. Committee has `ALLOW_MARKET_TRANSACTION` enabled (`supportAllowMarketTransaction() == true`).
2. User Alice broadcasts a `MarketSellAssetContract` transaction via `MarketSellAssetActuator`; validation passes, and her TRX/TRC10 balance is moved into the order book as a resting order (see `execute()` at [4](#0-3) ).
3. Committee passes a proposal to set `ALLOW_MARKET_TRANSACTION` to `0` (a normal governance action already supported by `ProposalUtil`/`ProposalService`).
4. Alice broadcasts a `MarketCancelOrderContract` transaction targeting her still-open order. `MarketCancelOrderActuator.validate()` throws `"Not support Market Transaction, need to be opened by the committee"` at line 169 before ever reaching the order-ownership/state checks, so `execute()` (which would call `MarketUtils.returnSellTokenRemain` to refund her locked balance) never runs.
5. Alice's locked TRX/TRC10 remains stuck in `MarketOrderStore`/`MarketAccountStore` with no unprivileged transaction available to release it until the committee re-enables the flag.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L100-151)
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

      orderStore.put(orderCapsule.getID().toByteArray(), orderCapsule);
      accountStore.put(accountCapsule.createDbKey(), accountCapsule);

      ret.setOrderId(orderCapsule.getID());
      ret.setStatus(fee, code.SUCESS);
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L181-184)
```java
    if (!dynamicStore.supportAllowMarketTransaction()) {
      throw new ContractValidateException("Not support Market Transaction, need to be opened by"
          + " the committee");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketCancelOrderActuator.java (L96-109)
```java
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

**File:** actuator/src/main/java/org/tron/core/actuator/MarketCancelOrderActuator.java (L168-171)
```java
    if (!dynamicStore.supportAllowMarketTransaction()) {
      throw new ContractValidateException("Not support Market Transaction, need to be opened by"
          + " the committee");
    }
```
