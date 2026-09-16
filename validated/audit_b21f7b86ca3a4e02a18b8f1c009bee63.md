### Title
Lost balance update when a user's own orders self-match in `MarketSellAssetActuator` - ([File: actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java])

### Summary
`MarketSellAssetActuator.execute()` keeps the taker's `AccountCapsule` in memory for the whole transaction and persists it only once at the end, while the maker side of every match is fetched fresh from `AccountStore` and persisted immediately. When a taker order matches a maker order that belongs to the **same account** (self-trade), the maker-side balance update gets silently discarded because the taker's stale in-memory object is written back last, overwriting the maker-side write.

### Finding Description
In `execute()`, the taker's account is loaded once, mutated in memory (fee deduction, sell-token transfer, buy-token credit), and written back to the store only at the very end: [1](#0-0) 

During matching, `matchSingleOrder` credits the taker via the in-memory object (`addTrxOrToken(takerOrderCapsule, ..., takerAccountCapsule)`), but credits the **maker** via a separate helper that re-reads the account from `AccountStore`, mutates it, and immediately `put`s it back: [2](#0-1) 

`matchOrder`/`matchSingleOrder` never checks whether the maker order's owner is the same as the taker's owner: [3](#0-2) [4](#0-3) 

If a single account happens to have a resting maker order that its own new taker order matches against, the sequence is:
1. `accountCapsule` (taker, same address) is loaded and mutated in memory for fee/sell-token deduction.
2. `matchOrder` -> `matchSingleOrder` -> `addTrxOrToken(makerOrderCapsule, makerBuyTokenQuantityReceive)` re-fetches the account for that same address **from the store** (a stale copy that doesn't include step 1's in-memory changes), adds the maker proceeds, and immediately persists it.
3. After `matchOrder` returns, `execute()` unconditionally does `accountStore.put(accountCapsule.createDbKey(), accountCapsule)` with the original in-memory taker object, which does not contain the maker-side credit applied in step 2 — overwriting and erasing it.

This is analogous to the reported issue: an "extra" balance adjustment (the maker-side proceeds) that legitimately belongs to the account is computed correctly but is not taken into account by the final state that gets committed, because the code path that determines the final persisted balance ignores a concurrent adjustment made to the same account within the same transaction.

### Impact Explanation
The maker-side proceeds of a self-match (TRX or TRC10 tokens) are permanently lost — deducted from the order book/maker order but never credited to the account's final balance, because the last write wins and does not include that credit. This is a concrete, permanent loss of user funds triggered purely by normal market order placement, not by any privileged or malicious third party.

### Likelihood Explanation
Any account can place two market orders (e.g., sell A for B and later sell B for A) that end up in the order book at compatible prices; the order matching engine performs no owner-based self-trade check, so it is trivial for a single unprivileged order placer to trigger this by opposing their own resting order at a matching price, causing loss of their own maker-side proceeds each time it happens (and could also be exploited to strand funds intentionally as a griefing/burn primitive).

### Recommendation
- Persist the maker's account update using the same up-to-date state as the taker when the maker and taker addresses are identical (e.g., detect same-owner match and apply both taker and maker adjustments to a single in-memory `AccountCapsule`, or defer all `AccountStore.put` calls to the end of `execute()` keyed by address so repeated updates to the same account accumulate instead of overwriting).
- Alternatively/additionally, prevent self-matching by rejecting or skipping maker orders whose owner equals the taker's owner in `hasMatch`/`matchOrder`.

### Proof of Concept
1. Account A creates a sell order O1: sell token X for token Y at price P (stored as a maker order in `pairPriceToOrderStore`/`orderStore`), via `MarketSellAssetActuator` (`createAndSaveOrder`).
2. Account A later creates a second sell order O2: sell token Y for token X at a price that matches O1 (i.e., `MarketUtils.priceMatch` returns true against O1's price).
3. On executing O2:
   - `accountCapsule` for A is loaded once at the top of `execute()`, and the fee + sell-token(Y) transfer are applied only in memory.
   - `matchOrder` finds O1 (owned by A) as the maker match and calls `matchSingleOrder`, which:
     - Adds the taker's (O2's) buy proceeds to the in-memory `accountCapsule` via `addTrxOrToken(takerOrderCapsule, ..., accountCapsule)`.
     - Adds the maker's (O1's) buy proceeds (token X) to A's account via `addTrxOrToken(makerOrderCapsule, ...)`, which re-reads A's account from `AccountStore` (still without the taker-side fee/sell deduction applied in step above) and immediately `accountStore.put`s the result.
   - After `matchOrder` returns, `execute()` executes `accountStore.put(accountCapsule.createDbKey(), accountCapsule)` using the original in-memory object — overwriting the store and erasing the maker-side token X credit added moments earlier.
4. Final result: A's account balance reflects the taker-side changes only; the maker-side proceeds credited for O1 are silently lost, even though O1's sold quantity (token X) was already deducted from A's balance/order-book state when O1 was created. This can be reproduced deterministically in a test harness such as `framework/src/test/java/org/tron/core/actuator/MarketSellAssetActuatorTest.java` by setting both the maker and taker order's owner address to the same account and asserting the final `AssetV2Map`/balance for token X against the expected sum of both fills.

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

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L307-347)
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
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L485-499)
```java
    // save makerOrderCapsule
    orderStore.put(makerOrderCapsule.getID().toByteArray(), makerOrderCapsule);

    // add token into account
    addTrxOrToken(takerOrderCapsule, takerBuyTokenQuantityReceive, takerAccountCapsule);
    addTrxOrToken(makerOrderCapsule, makerBuyTokenQuantityReceive);

    MarketOrderDetail orderDetail = MarketOrderDetail.newBuilder()
        .setMakerOrderId(makerOrderCapsule.getID())
        .setTakerOrderId(takerOrderCapsule.getID())
        .setFillSellQuantity(makerBuyTokenQuantityReceive)
        .setFillBuyQuantity(takerBuyTokenQuantityReceive)
        .build();
    ret.addOrderDetails(orderDetail);
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
