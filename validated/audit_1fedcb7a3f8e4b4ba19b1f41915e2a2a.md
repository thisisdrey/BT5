### Title
Self-matching a Market order silently destroys the maker-side settlement due to a stale `AccountCapsule` overwrite - (File: `actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java`)

### Summary
`MarketSellAssetActuator` fetches a single `AccountCapsule` for the taker at the start of `execute()`, mutates it in memory throughout order creation and matching, and only persists it to `AccountStore` once, at the very end. However, when settling the maker side of a fill, `addTrxOrToken(MarketOrderCapsule, long)` independently re-fetches the maker's account from the store and immediately writes it back. When the taker self-matches against their own resting order (maker address == taker address, which is not prohibited anywhere), two divergent in-memory copies of the same account get persisted, and the later write (the taker's copy) silently overwrites/clobbers the earlier maker-side credit — exactly the "stale temporary copy" bug class described in the external KIBToken report, where using a separately materialized balance snapshot causes lost or duplicated funds on self-interaction.

### Finding Description
In `execute()`, the taker's account object is loaded once [1](#0-0)  and only written back to `accountStore` at the end of the method [2](#0-1) .

Between those two points, `matchOrder` -> `matchSingleOrder` is invoked, which calls two different overloads of `addTrxOrToken`:
- One overload operates directly on the already-in-memory taker `AccountCapsule` instance (no separate store fetch/write) [3](#0-2) .
- The other overload (used for the maker side) independently fetches the account from `accountStore`, mutates it, and immediately persists it with `accountStore.put(...)` [4](#0-3) .

`matchSingleOrder` invokes both variants back-to-back for taker and maker respectively [5](#0-4) .

Nothing in `validate()` prevents the resting maker order's owner address from being identical to the taker's own address — `validate()` only checks that `sellTokenID != buyTokenID` [6](#0-5) , and `matchOrder`/`hasMatch` select maker orders purely by token pair and price without any owner-address exclusion [7](#0-6) .

If a single account has previously placed a resting sell order for pair (B→A) and now submits a new sell order for pair (A→B) that matches its own resting order, both the taker-side and maker-side settlement operate on the *same* underlying `AccountCapsule`, but through two different in-memory object instances:
1. `addTrxOrToken(makerOrderCapsule, makerReceive)` loads a fresh copy from the store (missing the fee/sell-side mutations already applied to the taker's in-memory copy), credits it with the maker-side proceeds, and writes it to the store immediately.
2. At the end of `execute()`, the taker's original in-memory copy (which never saw the maker-side credit) is written to the store, overwriting the balance/asset state set in step 1.

The net effect is the same "temporary variable overwrites the other side's update" pattern flagged in the report: an update applied to one materialized copy of an account's balance is discarded because a stale copy is written afterward.

### Impact Explanation
The maker-side proceeds from a self-matched fill (`makerBuyTokenQuantityReceive`, the tokens/TRX earned for the maker "selling to themself") are computed, added to a persisted copy of the account, and then silently discarded when the taker's stale in-memory copy overwrites the account record. This is a permanent, unrecoverable loss of the credited asset/TRX amount for the affected account — an unbacked-balance/fund-freezing condition triggerable purely through normal `MarketSellAssetContract` broadcasts, with no privileged access required.

### Likelihood Explanation
Any account can trivially construct this condition: place a sell order for pair (X→Y), let it rest in the order book (no match), then place a second sell order for pair (Y→X) at a matching price so the new order matches against their own resting order. `matchOrder`/`hasMatch` never checks that maker and taker addresses differ. This requires only two ordinary `MarketSellAssetContract` transactions from a single account and no cooperation from any other party, making it straightforward to trigger.

### Recommendation
- When the maker order's owner address equals the taker's address, operate on the single already-in-memory `AccountCapsule` instance instead of re-fetching from `accountStore`, applying both the taker-side and maker-side deltas to the same object before a single final persist.
- Alternatively, restructure settlement so that all balance/asset mutations for a transaction accumulate against one canonical, in-memory `Map<address, AccountCapsule>` cache that is flushed to the store exactly once per address at the end of `execute()`, eliminating the possibility of stale reads/writes when the same address appears on both sides of a match.

### Proof of Concept
1. Account `A` calls `MarketSellAssetContract` selling token `X` for token `Y` at price `p1`; the order does not fully match and rests in the order book (`saveRemainOrder`).
2. Account `A` calls `MarketSellAssetContract` selling token `Y` for token `X` at a price that matches the resting order from step 1.
3. Inside `matchSingleOrder`, `addTrxOrToken(takerOrderCapsule, takerReceive, accountCapsule)` credits the in-memory taker `AccountCapsule` for `A`, while `addTrxOrToken(makerOrderCapsule, makerReceive)` independently fetches, credits, and persists a separate `AccountCapsule` instance for the same address `A` [4](#0-3) .
4. `execute()` then persists the taker's in-memory `AccountCapsule` at line 148, overwriting the maker-side credit written in step 3 [2](#0-1) .
5. Result: account `A`'s final balance/asset amount for token `X` (the maker-side proceeds) is missing the `makerBuyTokenQuantityReceive` amount that should have been credited, permanently losing those funds.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L114-115)
```java
      AccountCapsule accountCapsule = accountStore
          .get(contract.getOwnerAddress().toByteArray());
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L147-148)
```java
      orderStore.put(orderCapsule.getID().toByteArray(), orderCapsule);
      accountStore.put(accountCapsule.createDbKey(), accountCapsule);
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L219-221)
```java
    if (Arrays.equals(sellTokenID, buyTokenID)) {
      throw new ContractValidateException("cannot exchange same tokens");
    }
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

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L488-490)
```java
    // add token into account
    addTrxOrToken(takerOrderCapsule, takerBuyTokenQuantityReceive, takerAccountCapsule);
    addTrxOrToken(makerOrderCapsule, makerBuyTokenQuantityReceive);
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L538-548)
```java
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
