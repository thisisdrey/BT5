Confirmed: `validate()` in `MarketSellAssetActuator` has no check preventing an account from placing a taker order that matches its own resting maker order — there's no self-trade restriction, only a check that `sellTokenID != buyTokenID`.

This confirms the bug: self-trading (an account's taker order matching its own earlier maker order) causes the maker-side settlement write to be silently overwritten and lost, due to stale in-memory account object usage.

### Title
Self-matching orders cause permanent loss of maker-side settlement funds due to stale in-memory account overwrite - (File: actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java)

### Summary
`MarketSellAssetActuator.execute()` and its helper `matchSingleOrder`/`addTrxOrToken` handle taker and maker account balance updates inconsistently when the taker and maker of a matched trade are the same account (self-trade). The maker-side credit is persisted via an immediate `accountStore.put()` inside `addTrxOrToken(MarketOrderCapsule, long)`, using a freshly-loaded `AccountCapsule`, but this write is subsequently clobbered by the final `accountStore.put(accountCapsule.createDbKey(), accountCapsule)` in `execute()`, which uses the stale in-memory taker `AccountCapsule` object that never observed the maker-side credit.

### Finding Description
In `execute()` [1](#0-0) , a single `AccountCapsule accountCapsule` object (the taker/owner) is loaded once, mutated in memory across `transferBalanceOrToken`, `createAndSaveOrder`, and `matchOrder`, and only written back to `accountStore` at the very end.

Inside `matchOrder` → `matchSingleOrder`, when a match occurs, both sides are credited via `addTrxOrToken`: the taker side uses the in-memory `takerAccountCapsule` object (no immediate store write) [2](#0-1) , while the maker side uses a separate overload that re-fetches the account from `accountStore` by `orderCapsule.getOwnerAddress()`, mutates it, and immediately calls `accountStore.put(...)` [3](#0-2) .

If the maker order being matched belongs to the *same address* as the taker (self-trade — an account matching against its own resting order, which nothing in `validate()` prevents; the only related check is `sellTokenID != buyTokenID`, see lines 219-221) [4](#0-3) , then:
1. The maker-side `addTrxOrToken` call fetches the account from the store (reflecting state *before* this transaction's in-memory changes are persisted) and writes the maker-side credit immediately.
2. Back in `execute()`, the final `accountStore.put(accountCapsule.createDbKey(), accountCapsule)` overwrites the store with the stale taker-side object, which reflects the fee deduction, the sell-side debit, and the taker-side credit, but **not** the maker-side credit that step 1 just wrote [5](#0-4) .

The net result is that the maker-side proceeds from the trade are silently discarded — permanently lost — for any account whose new sell order matches one of its own outstanding orders in the order book.

### Impact Explanation
This causes a concrete, permanent loss of funds for the account involved in the self-match: the tokens/TRX it should have received as the maker of the trade vanish from its balance, since they are computed and written but then immediately overwritten. Because this can be reliably triggered by any unprivileged account through the public `MarketSellAssetContract` (broadcastable via `MarketSellAssetActuator`), and it can occur unintentionally (e.g., a user's own resting order matches their new order due to price movement or an automated market-making bot), it can also be exploited to deliberately zero out or manipulate liquidity/order books without an attacker directly benefiting from the "stolen" side, effectively burning assets and corrupting account balances.

### Likelihood Explanation
Likelihood is high: no signer other than the order owner is needed, and there is no check in `validate()` preventing a taker order from matching the same account's maker order. Any account with two resting/overlapping price orders (which is a legitimate use-case, e.g., adjusting price by re-submitting orders) can trigger this unintentionally, and an attacker can trivially construct it deliberately by placing a maker order and then a matching taker order from the same account.

### Recommendation
1. In `matchSingleOrder`, ensure both taker- and maker-side account state updates go through a single, consistent path — e.g., detect when `takerAccountCapsule`'s owner address equals the maker order's owner address, and in that case apply both credits to the same in-memory object before any store write, instead of independently re-fetching and immediately persisting the maker's account.
2. Alternatively, restructure `execute()`/`matchOrder` to defer all `accountStore.put()` calls to a single point per unique address at the end of processing, using a per-address cache of `AccountCapsule` objects that is consulted for both taker and maker credits.
3. Add explicit unit tests that create a maker order and a matching taker order from the same owner address and assert that the resulting balance equals the sum of both maker- and taker-side proceeds.

### Proof of Concept
1. Account `A` places `addOrder(TOKEN_X, 100, TOKEN_Y, 100, A)` — creates a resting maker order selling `TOKEN_X` for `TOKEN_Y`.
2. Account `A` then submits a `MarketSellAssetContract` selling `TOKEN_Y` for `TOKEN_X` at a matching price (i.e., `sellTokenId = TOKEN_Y`, `buyTokenId = TOKEN_X`), which the `hasMatch`/`matchOrder` logic pairs against `A`'s own resting order from step 1.
3. `execute()` runs: `transferBalanceOrToken` debits `TOKEN_Y` from `A`'s in-memory `accountCapsule`; `matchSingleOrder` computes maker/taker fills; `addTrxOrToken(makerOrderCapsule, makerBuyTokenQuantityReceive)` (the single-account-address version) re-fetches `A`'s account from the store, credits it with `TOKEN_Y` (maker proceeds), and immediately calls `accountStore.put`.
4. `execute()` finishes with `accountStore.put(accountCapsule.createDbKey(), accountCapsule)`, writing the taker-side in-memory object for `A` (which only has the `TOKEN_X` taker credit and the `TOKEN_Y` debit, fee subtraction) — overwriting the maker-side `TOKEN_Y` credit from step 3.
5. Final balance check: `A`'s `TOKEN_Y` balance is short by the maker-side proceeds amount compared to what correct bookkeeping should yield, demonstrating permanent fund loss.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L114-148)
```java
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
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L219-221)
```java
    if (Arrays.equals(sellTokenID, buyTokenID)) {
      throw new ContractValidateException("cannot exchange same tokens");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L537-548)
```java
  // for taker
  private void addTrxOrToken(MarketOrderCapsule orderCapsule, long num,
      AccountCapsule accountCapsule) {

    byte[] buyTokenId = orderCapsule.getBuyTokenId();
    if (Arrays.equals(buyTokenId, "_".getBytes())) {
      accountCapsule.setBalance(addExact(accountCapsule.getBalance(), num));
    } else {
      accountCapsule
          .addAssetAmountV2(buyTokenId, num, dynamicStore, assetIssueStore);
    }
  }
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
