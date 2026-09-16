### Title
Market trading kill-switch (`ALLOW_MARKET_TRANSACTION`) permanently locks user-escrowed order funds with no cancellation path - ([File: actuator/src/main/java/org/tron/core/actuator/MarketCancelOrderActuator.java])

### Summary
Both order placement (`MarketSellAssetActuator`) and order cancellation (`MarketCancelOrderActuator`) gate their `validate()` on the single dynamic parameter `supportAllowMarketTransaction()`. When a resting order exists (funds already deducted from the owner's balance and held in the order book), the only code path to return those funds to the user — `MarketCancelOrderActuator` — is itself disabled the moment the committee turns market trading off. This reproduces the exact catch-22 described in the referenced report: once a critical bug is found in the market-matching logic and the committee must disable the feature to stop further loss, every user with an unmatched resting order permanently loses access to their escrowed TRX/TRC10 tokens until (if ever) the committee re-enables the feature — and even then, the underlying bug that forced the shutdown is still exploitable.

### Finding Description
`MarketSellAssetActuator.execute()` deducts the sell-side balance/asset from the caller and stores the remainder in a `MarketOrderCapsule` in the order book when there's no immediate match: [1](#0-0) 

Placing that order is only allowed while trading is enabled: [2](#0-1) 

The only actuator capable of returning the escrowed remainder back to the owner, `MarketCancelOrderActuator`, calls `MarketUtils.returnSellTokenRemain(...)` in `execute()`: [3](#0-2) [4](#0-3) 

But `MarketCancelOrderActuator.validate()` requires the exact same flag that gates order placement: [5](#0-4) 

`supportAllowMarketTransaction()` is a committee-controlled dynamic parameter (`ALLOW_MARKET_TRANSACTION`), toggled via `ProposalCreate`/`ProposalApprove` governance actuators and consumed through `DynamicPropertiesStore`, per `ProposalUtil.java` and `ProposalService.java`. There is no matching-engine "auto-settlement" or block-level sweep that returns escrow when the parameter is off — cancellation is the sole return path, and it is disabled together with placement.

### Impact Explanation
If the committee ever needs to disable `ALLOW_MARKET_TRANSACTION` (e.g., in response to a discovered exploit in the price/matching logic, arithmetic overflow in `MarketUtils.multiplyAndDivide`, or any other market-actuator bug), every account with an outstanding unmatched (or partially matched) sell order at that moment has its remaining escrowed TRX or TRC10 tokens (`sellTokenQuantityRemain`) frozen with no available transaction path to reclaim them. This is a genuine, protocol-level permanent freezing-of-funds condition for any unprivileged order placer, reachable purely by placing a market order before governance disables the feature. It also forces the committee into the same dilemma as the original report: either leave the buggy market feature enabled (continued risk) or disable it (guaranteed freezing of every open order's escrow).

### Likelihood Explanation
Likelihood is moderate: it requires the committee to disable trading via a super-representative-approved proposal, which is a normal, expected governance action (used historically to pause TRC10 exchange/market functionality when bugs are found). Any user can trivially arrange to hold an open resting order (a single unprivileged `MarketSellAssetContract` broadcast) at the time such a pause occurs, making the affected population effectively "all users with resting orders" whenever the switch is flipped off.

### Recommendation
Decouple fund-return operations from the trading kill-switch: `MarketCancelOrderActuator.validate()` should not require `supportAllowMarketTransaction()` to be true — cancellation of one's own existing order and return of escrowed funds should always be permitted regardless of whether new order placement/matching is currently enabled. Alternatively, add an explicit "wind-down" mode that keeps cancellation open while blocking new order creation and matching.

### Proof of Concept
1. Committee proposal enables `ALLOW_MARKET_TRANSACTION` (`ProposalUtil`/`ProposalService`).
2. Alice broadcasts `MarketSellAssetContract` (sell TOKEN_A for TOKEN_B) via `MarketSellAssetActuator`; no immediate match exists, so her TOKEN_A is deducted from her account and stored as `sellTokenQuantityRemain` in the resting `MarketOrderCapsule` (see `MarketSellAssetActuator.execute()` lines 133-145).
3. The committee passes a proposal disabling `ALLOW_MARKET_TRANSACTION` (e.g., in reaction to a discovered market bug).
4. Alice broadcasts `MarketCancelOrderContract` to reclaim her escrowed TOKEN_A. `MarketCancelOrderActuator.validate()` throws `"Not support Market Transaction, need to be opened by the committee"` (line 168-171) — the transaction fails validation and cannot execute.
5. Alice's TOKEN_A remains locked in the order/escrow indefinitely, with no other actuator or API path capable of returning it, until/unless the committee re-enables the exact feature that necessitated the shutdown.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L133-145)
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
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L181-184)
```java
    if (!dynamicStore.supportAllowMarketTransaction()) {
      throw new ContractValidateException("Not support Market Transaction, need to be opened by"
          + " the committee");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketCancelOrderActuator.java (L103-109)
```java
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

**File:** chainbase/src/main/java/org/tron/core/capsule/utils/MarketUtils.java (L280-295)
```java
  public static void returnSellTokenRemain(MarketOrderCapsule orderCapsule,
      AccountCapsule accountCapsule,
      DynamicPropertiesStore dynamicStore,
      AssetIssueStore assetIssueStore) {
    byte[] sellTokenId = orderCapsule.getSellTokenId();
    long sellTokenQuantityRemain = orderCapsule.getSellTokenQuantityRemain();
    if (Arrays.equals(sellTokenId, "_".getBytes())) {
      accountCapsule.setBalance(addExact(
          accountCapsule.getBalance(), sellTokenQuantityRemain,
          dynamicStore.disableJavaLangMath()));
    } else {
      accountCapsule
          .addAssetAmountV2(sellTokenId, sellTokenQuantityRemain, dynamicStore, assetIssueStore);
    }
    orderCapsule.setSellTokenQuantityRemain(0L);
  }
```
