### Title
Self-trading against one's own resting order in `MarketSellAssetActuator` silently burns the maker-side proceeds due to stale `AccountCapsule` cache overwrite - (File: actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java)

### Summary
`MarketSellAssetActuator` (the actuator that services the TRC10 `MarketSellAssetContract`, reachable by any account that submits a signed `MarketSellAssetContract` transaction) keeps a single in-memory `AccountCapsule` for the taker for the duration of `execute()`, but for the maker side of every matched order it independently re-reads a **fresh** copy of the account from `AccountStore` and immediately persists it. When the taker and the maker are the same account (a user's new order matches one of their own still-resting orders — nothing in `validate()` forbids this), the two in-memory copies diverge and the taker's stale copy is written last, silently discarding the maker-side credit that was already written to the store. This is the same root-cause pattern as the reported `AllocationVesting::transferPoints` bug: a self-operation is not special-cased, so a stale memory cache of the "other side" of the operation clobbers the freshly-written state, this time causing loss/destruction of a user's own token balance rather than duplication.

### Finding Description
In `MarketSellAssetActuator.execute()`, a single `AccountCapsule accountCapsule` for the taker (owner of the new order) is loaded once, fee/sell-token debited in memory, and only persisted at the very end: [1](#0-0) 

During matching, `matchSingleOrder()` credits the taker using the passed-in, still in-memory `takerAccountCapsule`, but credits the maker through a completely separate code path that re-reads and re-persists the account independently: [2](#0-1) 

```java
// for maker
private void addTrxOrToken(MarketOrderCapsule orderCapsule, long num) {
  AccountCapsule accountCapsule = accountStore
      .get(orderCapsule.getOwnerAddress().toByteArray());
  ...
  accountStore.put(orderCapsule.getOwnerAddress().toByteArray(), accountCapsule);
}
``` [3](#0-2) 

`validate()` never checks that the maker order's owner differs from the taker's owner address, unlike `TransferActuator`, `TransferAssetActuator`, `DelegateResourceActuator`, `UnDelegateResourceActuator` and `ShieldedTransferActuator`, all of which explicitly forbid self-transfer/self-delegation: [4](#0-3) 

So a user can create a resting sell order, then later place a new order that matches it. In that single `execute()` call:
1. The taker's `accountCapsule` (in memory) is debited fee + sell quantity, then credited the taker-side proceeds.
2. `addTrxOrToken(makerOrderCapsule, num)` re-reads the account from `AccountStore` — this read returns the account state *before* step 1 was applied (step 1 is only in memory, not yet flushed) — credits the maker-side proceeds to that stale copy, and immediately calls `accountStore.put(...)`.
3. At the end of `execute()`, `accountStore.put(accountCapsule.createDbKey(), accountCapsule)` runs unconditionally with the taker's in-memory copy from step 1, overwriting the write from step 2.

The net effect: the maker-side proceeds credited in step 2 are permanently lost from the persisted account state — an unbacked reduction (destruction) of the trading account's own tokens/TRX, with no corresponding balance appearing anywhere else in the ledger.

### Impact Explanation
This causes **permanent, silent destruction of a user's own on-chain assets (TRX or TRC10 tokens)** any time a self-match occurs in the on-chain order book — assets vanish from total accounted supply without being credited to the blackhole or any other account, i.e. an unbacked-balance/permanent-loss-of-funds condition triggerable purely by normal, unprivileged order-placement transactions. Because order matching happens automatically against the resting order book, this can be triggered unintentionally by ordinary users (e.g., placing a new order that happens to cross their own earlier resting order) or deliberately to grief/burn balances, and it can occur repeatedly across multiple matches within one `matchOrder()` loop, all of which get discarded by the same final overwrite.

### Likelihood Explanation
Reaching this path only requires two ordinary, unprivileged `MarketSellAssetContract` transactions from the same account: one to place a resting order, and a later one whose token pair/price matches it (`validate()` places no restriction preventing this). No special privileges, committee approval, or victim cooperation are needed, and the market-transaction feature is a normal user-facing feature (guarded only by the `supportAllowMarketTransaction` chain parameter, which is expected to be enabled for the feature to be usable at all).

### Recommendation
- In `MarketSellAssetActuator`, avoid re-reading the account from `AccountStore` for the maker side; instead pass and reuse the same `AccountCapsule` instance (or explicitly detect `Arrays.equals(takerOwnerAddress, makerOrderCapsule.getOwnerAddress().toByteArray())` and route the maker-side credit through the already-in-memory taker capsule so all changes are merged into one object before the single final `accountStore.put()`).
- Apply the same fix to the analogous single-argument `returnSellTokenRemain(MarketOrderCapsule)` helper, which has the same stale-read/immediate-write pattern.
- Add regression tests where a taker's new order matches one or more of the taker's own resting orders, asserting that the final persisted balance/asset amounts reflect all debits and credits (taker debit, taker credit, and maker credit) with no discarded state.

### Proof of Concept
1. Account `A` places `MarketSellAssetContract` order O1: sell 100 of Token X for 100 of Token Y. This persists O1 in `orderStore` and debits 100 X from `A`'s stored balance (via the normal `execute()` flow, unrelated self-trade path).
2. Later, account `A` places a second `MarketSellAssetContract` order O2: sell 100 of Token Y for 100 of Token X, priced to match O1.
3. In `execute()` for O2:
   - `accountCapsule` for `A` is loaded (call it `S0`), fee and 100 Y debited in memory → `S1 = S0 - fee - 100Y`.
   - `matchOrder()`/`matchSingleOrder()` runs; O1 (owned by the same `A`) is selected as maker.
   - `addTrxOrToken(takerOrderCapsule, 100X, takerAccountCapsule)` credits 100 X to the in-memory `S1` → `S2 = S1 + 100X`.
   - `addTrxOrToken(makerOrderCapsule, 100Y)` re-reads `A`'s account fresh from `AccountStore` (returns `S0`, since `S1`/`S2` were never flushed), credits 100 Y → `S0 + 100Y`, and immediately calls `accountStore.put(A, S0 + 100Y)`.
   - After `matchOrder()` returns, `execute()` unconditionally calls `accountStore.put(A, accountCapsule)` where `accountCapsule == S2 = S0 - fee - 100Y + 100X`, overwriting the previous store write.
4. Final persisted state for `A`: `S0 - fee - 100Y + 100X`. The 100 Y that should have been returned to `A` as the maker of O1 (offsetting the earlier debit when O1 was created) is permanently missing from the ledger — it was written to the store and then clobbered, resulting in a net, unrecoverable loss of 100 Y for account `A` with no corresponding credit anywhere else in the system.

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

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L164-230)
```java
  @Override
  public boolean validate() throws ContractValidateException {
    if (this.any == null) {
      throw new ContractValidateException(CONTRACT_NOT_EXIST);
    }
    if (chainBaseManager == null) {
      throw new ContractValidateException(STORE_NOT_EXIST);
    }

    initStores();

    if (!this.any.is(MarketSellAssetContract.class)) {
      throw new ContractValidateException(
          "contract type error,expected type [MarketSellAssetContract],real type[" + any
              .getClass() + "]");
    }

    if (!dynamicStore.supportAllowMarketTransaction()) {
      throw new ContractValidateException("Not support Market Transaction, need to be opened by"
          + " the committee");
    }

    final MarketSellAssetContract contract;
    try {
      contract =
          this.any.unpack(MarketSellAssetContract.class);
    } catch (InvalidProtocolBufferException e) {
      logger.debug(e.getMessage(), e);
      throw new ContractValidateException(e.getMessage());
    }

    // Parameters check
    byte[] ownerAddress = contract.getOwnerAddress().toByteArray();
    sellTokenID = contract.getSellTokenId().toByteArray();
    buyTokenID = contract.getBuyTokenId().toByteArray();
    sellTokenQuantity = contract.getSellTokenQuantity();
    buyTokenQuantity = contract.getBuyTokenQuantity();

    if (!DecodeUtil.addressValid(ownerAddress)) {
      throw new ContractValidateException("Invalid address");
    }

    // Whether the accountStore exist
    AccountCapsule ownerAccount = accountStore.get(ownerAddress);
    if (ownerAccount == null) {
      throw new ContractValidateException("Account does not exist!");
    }

    if (!Arrays.equals(sellTokenID, "_".getBytes()) && !isNumber(sellTokenID)) {
      throw new ContractValidateException("sellTokenId is not a valid number");
    }
    if (!Arrays.equals(buyTokenID, "_".getBytes()) && !isNumber(buyTokenID)) {
      throw new ContractValidateException("buyTokenId is not a valid number");
    }

    if (Arrays.equals(sellTokenID, buyTokenID)) {
      throw new ContractValidateException("cannot exchange same tokens");
    }

    if (sellTokenQuantity <= 0 || buyTokenQuantity <= 0) {
      throw new ContractValidateException("token quantity must greater than zero");
    }

    long quantityLimit = dynamicStore.getMarketQuantityLimit();
    if (sellTokenQuantity > quantityLimit || buyTokenQuantity > quantityLimit) {
      throw new ContractValidateException("token quantity must less than " + quantityLimit);
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L485-490)
```java
    // save makerOrderCapsule
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
