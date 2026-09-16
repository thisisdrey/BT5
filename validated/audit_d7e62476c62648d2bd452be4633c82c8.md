### Title
Disabling market transactions via committee proposal permanently locks tokens already escrowed in active market orders - (File: `actuator/src/main/java/org/tron/core/actuator/MarketCancelOrderActuator.java`)

### Summary
`MarketSellAssetActuator` (order placement) and `MarketCancelOrderActuator` (order cancellation) are the only two actuators that can move a user's sell-token balance out of the on-chain order book once it has been escrowed by placing a market order. Both actuators gate their `validate()` on the same dynamic-property flag, `supportAllowMarketTransaction()`. If the committee toggles this flag off after users have already placed active orders (tokens already deducted from their account balance and held in `MarketOrderCapsule`/`MarketAccountStore`), those users lose the only way to retrieve their escrowed tokens, mirroring the reported "removing collateral locks tokens" bug class.

### Finding Description
When a user calls `MarketSellAssetActuator.execute`, their sell tokens are transferred out of their account balance into the order book (`transferBalanceOrToken`, order creation, and persistence into `MarketOrderStore`/`MarketPairPriceToOrderStore`) <cite repo="Loderfordw/java-tron--011" path="actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java" start="133-148" end="133-148" />.

The only way to get unmatched (or partially matched) tokens back out of that escrow is via `MarketCancelOrderActuator`, which returns the remaining sell tokens to the owner's account and removes the order from the book <cite repo="Loderfordw/java-tron--011" path="actuator/src/main/java/org/tron/core/actuator/MarketCancelOrderActuator.java" start="103-121" end="103-121" />.

However, `MarketCancelOrderActuator.validate()` requires `dynamicStore.supportAllowMarketTransaction()` to be `true`, throwing `"Not support Market Transaction, need to be opened by the committee"` otherwise <cite repo="Loderfordw/java-tron--011" path="actuator/src/main/java/org/tron/core/actuator/MarketCancelOrderActuator.java" start="168-171" end="168-171" />.

This is the exact same gate used to allow *creating* new orders in `MarketSellAssetActuator`, confirming the flag is meant to control the whole market feature, not just order creation. Because both entry (`MarketSellAssetActuator`) and exit (`MarketCancelOrderActuator`, and the automatic matching inside order placement) share this single committee-controlled switch, disabling the feature after orders are already active removes all remaining paths for a user to retrieve tokens already committed to the order book — there is no "withdraw" or "force-cancel" function that bypasses the flag, and matching against new incoming orders cannot happen either since new order placement is blocked by the same check. This directly parallels the USSD report: an admin/governance action ("removing collateral" / here, "disabling `ALLOW_MARKET_TRANSACTION`") strands funds that were deposited under the now-disabled feature, with no alternate withdrawal path in the contract.

### Impact Explanation
Any user with an active, unmatched (or partially matched) market order at the time the committee disables the market-transaction feature permanently loses access to the remaining escrowed sell-token balance, since cancellation — the only mechanism to reclaim it — is blocked by the same flag. This is a permanent freezing-of-funds condition for affected users, satisfying the Medium/High impact bar (permanent loss of access to already-deposited assets) even though the disabling action itself is a legitimate committee/governance operation, not a compromise.

### Likelihood Explanation
`ALLOW_MARKET_TRANSACTION` is a standard proposal-controlled dynamic parameter that the committee can legitimately toggle at any time via the normal proposal mechanism (`ProposalUtil`/`ProposalService`) [1](#0-0) . Any period where active orders exist in the book and this feature is turned off (e.g., to pause the market during an incident, exactly the kind of scenario that motivates disabling a risky feature) will trigger this freeze. No malicious actor is required — this is a foreseeable consequence of intended governance actions, matching the analog's own scenario ("perhaps if it turns out to be too volatile to be used").

### Recommendation
Decouple the ability to cancel/withdraw already-escrowed orders from the `supportAllowMarketTransaction` flag used to gate new order creation and matching. `MarketCancelOrderActuator.validate()` should not require `supportAllowMarketTransaction()` to be true — users must always be able to cancel their own outstanding orders and recover their escrowed tokens regardless of whether new market activity is currently enabled.

### Proof of Concept
1. Committee proposal enables `ALLOW_MARKET_TRANSACTION`.
2. User A broadcasts a `MarketSellAssetContract` via `MarketSellAssetActuator`; tokens are debited from A's account and escrowed as an active `MarketOrder` (assume no matching counter-order exists, so the full amount remains, per `MarketSellAssetActuator.execute` lines 133–148) [2](#0-1) .
3. Committee submits and approves a proposal to disable `ALLOW_MARKET_TRANSACTION`.
4. User A broadcasts `MarketCancelOrderContract` to reclaim escrowed tokens; `MarketCancelOrderActuator.validate()` throws `ContractValidateException("Not support Market Transaction, need to be opened by the committee")` [3](#0-2) .
5. User A's tokens remain permanently locked in the order book with no other contract-level path to withdraw them.

### Citations

**File:** actuator/src/main/java/org/tron/core/utils/ProposalUtil.java (L1-1)
```java
package org.tron.core.utils;
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L133-148)
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
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketCancelOrderActuator.java (L168-171)
```java
    if (!dynamicStore.supportAllowMarketTransaction()) {
      throw new ContractValidateException("Not support Market Transaction, need to be opened by"
          + " the committee");
    }
```
