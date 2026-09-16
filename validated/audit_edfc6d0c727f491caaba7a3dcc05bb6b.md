## Analog Found

### Title
Self-trade order matching causes silent loss of matched token/TRX proceeds due to a stale-account overwrite (lost update) - ([File: actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java])

### Summary
The CVE describes a use-after-free triggered when protocol messages arrive in an order the code did not anticipate, causing stale/freed state to be used. The analogous bug class in java-tron's on-chain order-matching engine is a **stale-state / lost-update** bug: `MarketSellAssetActuator` holds one in-memory `AccountCapsule` for the taker for the whole duration of `execute()`, but re-reads a *separate, independently-fetched* copy of the account from `AccountStore` for each matched maker order and immediately persists it. When the taker and a matched maker order belong to the **same account** (a legitimate, unprivileged self-trade), the two code paths race against the same underlying store record, and the taker's final, stale in-memory write silently erases the maker-side credit that was already persisted mid-execution.

### Finding Description
In `execute()`, the taker's account object is fetched once at the top and never re-read from the store for the rest of the transaction: [1](#0-0) 

That same `accountCapsule` instance is threaded through as `takerAccountCapsule` into `matchOrder()` → `matchSingleOrder()`, and is only committed back to the store at the very end of `execute()`: [2](#0-1) 

However, whenever a maker order is fully consumed, `matchOrder()` credits the maker's proceeds using the single-argument `addTrxOrToken(orderCapsule, num)` overload, which independently re-reads the account **from the store** (not from the in-memory `takerAccountCapsule`) and writes it back immediately: [3](#0-2) [4](#0-3) 

If the maker order that gets matched belongs to the *same address* as the taker (a self-trade — fully permitted, since `MarketSellAssetActuator`/order matching has no anti-self-trade check anywhere in `validate()` or `matchOrder()`), the sequence becomes:
1. `accountCapsule` (taker) is fetched, fee-deducted, sell-token-transferred, and buy-token additions from taker-side fills are applied in memory only.
2. During matching, `addTrxOrToken(makerOrderCapsule, makerBuyTokenQuantityReceive)` fetches the **still-unmodified, pre-transaction** copy of the very same account from `AccountStore`, adds the maker proceeds, and immediately `put()`s it — this commit is based on stale (pre-fee, pre-transfer) data.
3. At the end of `execute()`, the original stale `accountCapsule` (taker view, which never saw the maker credit from step 2) is written with `accountStore.put(accountCapsule.createDbKey(), accountCapsule)`, unconditionally overwriting the record from step 2.

The net effect: the maker-side proceeds credited in step 2 are permanently lost — tokens/TRX that were already debited from the counter-party side of the self-trade (asset removed via `reduceAssetAmountV2` for the sell side) never make it into the final persisted account state.

### Impact Explanation
Any account that places overlapping buy/sell orders for the same asset pair (or matches its own resting order) loses the value of the maker-side leg of the self-trade — the tokens/TRX are effectively burned from the user's own balance without any compensating burn/black-hole accounting, since `MarketUtils`/`AssetIssueStore` supply bookkeeping is untouched. This is a fund-loss bug reachable by any ordinary order placer with no special privileges, matching the "permanent loss of funds / unbacked balance" impact category. It can also be weaponized as a griefing/DoS vector against a victim's own funds if triggered unknowingly by naive market-making bots that place both sides of a pair from one wallet.

### Likelihood Explanation
The trigger requires only two ordinary, unprivileged `MarketSellAssetContract` broadcasts from the same account for opposite sides of a token pair such that they cross in price — a routine market-making/arbitrage pattern that requires no validator collusion, no protocol-level flag, and no special account permissions. `validate()` in `MarketSellAssetActuator`/`MarketCancelOrderActuator` contains no self-trade restriction, so the condition is trivially reachable in production usage.

### Recommendation
- Add an explicit self-trade guard (reject or auto-cancel) in `MarketSellAssetActuator.matchOrder()`/`matchSingleOrder()` when `makerOrderCapsule.getOwnerAddress()` equals the taker's owner address, or
- Refactor `addTrxOrToken(MarketOrderCapsule, long)` to route maker-side credits through the same in-memory `takerAccountCapsule` object whenever the addresses match (i.e., maintain a single account-object cache keyed by address for the duration of `execute()`, flushed once at the end), eliminating the read-modify-write race between the taker's deferred write and the maker's immediate write.

### Proof of Concept
1. Account `A` places `MarketSellAssetContract` selling `TokenX` for `TokenY` at price P1 (creates a resting maker order, `A`'s balance is debited `TokenX`).
2. Account `A` (same account) places a second `MarketSellAssetContract` selling `TokenY` for `TokenX` at a price that crosses P1, triggering `matchOrder()` to match against its own resting order from step 1 as taker.
3. Inside `matchSingleOrder()`, the maker order (step 1's order, owned by `A`) is fully consumed, calling `addTrxOrToken(makerOrderCapsule, makerBuyTokenQuantityReceive)` — this reads `A`'s pre-transaction balance from `AccountStore`, adds the `TokenX` proceeds, and commits immediately.
4. `execute()` finishes and unconditionally writes the original (stale) in-memory `accountCapsule` for `A`, overwriting the commit from step 3.
5. Result: account `A` never receives the `TokenX` proceeds credited in step 3 — those tokens are irrecoverably lost from `A`'s balance, verifiable by comparing `AccountStore.get(A).getAssetV2MapForTest()` before/after the two transactions against the expected proceeds from the match.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L114-120)
```java
      AccountCapsule accountCapsule = accountStore
          .get(contract.getOwnerAddress().toByteArray());

      sellTokenID = contract.getSellTokenId().toByteArray();
      buyTokenID = contract.getBuyTokenId().toByteArray();
      sellTokenQuantity = contract.getSellTokenQuantity();
      buyTokenQuantity = contract.getBuyTokenQuantity();
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L142-148)
```java
      // 4. save remain order into order book
      if (orderCapsule.getSellTokenQuantityRemain() != 0) {
        saveRemainOrder(orderCapsule);
      }

      orderStore.put(orderCapsule.getID().toByteArray(), orderCapsule);
      accountStore.put(accountCapsule.createDbKey(), accountCapsule);
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L486-490)
```java
    orderStore.put(makerOrderCapsule.getID().toByteArray(), makerOrderCapsule);

    // add token into account
    addTrxOrToken(takerOrderCapsule, takerBuyTokenQuantityReceive, takerAccountCapsule);
    addTrxOrToken(makerOrderCapsule, makerBuyTokenQuantityReceive);
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L550-562)
```java
  private void addTrxOrToken(MarketOrderCapsule orderCapsule, long num) {
    AccountCapsule accountCapsule = accountStore
        .get(orderCapsule.getOwnerAddress().toByteArray());

    byte[] buyTokenId = orderCapsule.getBuyTokenId();
    if (Arrays.equals(buyTokenId, "_".getBytes())) {
      accountCapsule.setBalance(addExact(accountCapsule.getBalance(), num));
    } else {
      accountCapsule
          .addAssetAmountV2(buyTokenId, num, dynamicStore, assetIssueStore);
    }
    accountStore.put(orderCapsule.getOwnerAddress().toByteArray(), accountCapsule);
  }
```
