Confirmed: `validate()` never checks that the taker's `ownerAddress` differs from the owner of any maker orders it may match against. Self-trading against one's own resting order is permitted.

### Title
Self-trade in `MarketSellAssetActuator.matchOrder` lets a stale in-memory `AccountCapsule` overwrite (erase) balance/asset updates performed via a separate store fetch on the same account, causing permanently lost funds - (File: `actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java`)

### Summary
`MarketSellAssetActuator.execute()` loads the taker's `AccountCapsule` once at the top, mutates it in memory as the order is created/filled, and finally writes it back to `accountStore` at the very end of `execute()`. Meanwhile, `matchSingleOrder()` credits the **maker** side of each fill via `addTrxOrToken(makerOrderCapsule, num)` (the no-capsule overload), which independently re-fetches the maker's account straight from `accountStore` and immediately `put`s it back. Because `validate()` places no restriction preventing a taker from matching against their own resting maker order (self-trade), when taker == maker the two code paths operate on two different in-memory snapshots of the same account. The final unconditional `accountStore.put(accountCapsule.createDbKey(), accountCapsule)` in `execute()` (using the stale taker snapshot) clobbers the interim maker-side credit that was already committed to the store, permanently destroying the token/TRX that should have been credited to the maker fill.

### Finding Description
- `execute()` fetches `accountCapsule` for the owner/taker once: [1](#0-0) 
- It is passed by reference through order creation and matching: [2](#0-1) 
- Inside `matchSingleOrder`, the taker side updates the passed-in capsule in memory only (`addTrxOrToken(takerOrderCapsule, ..., takerAccountCapsule)`), while the maker side re-fetches straight from the store and commits immediately: [3](#0-2) 
- The maker-crediting helper independently loads/stores the account: [4](#0-3) 
- `validate()` checks token validity, balances, order counts, etc., but never compares the taker's `ownerAddress` to the maker order owners it may subsequently match against, i.e., self-trading is not rejected: [5](#0-4) 
- Finally, `execute()` blindly overwrites the account record with the stale in-memory taker snapshot, discarding any interim store mutation that happened to the same account via the maker path: [6](#0-5) 

This mirrors the structural root cause of the Penpie exploit: an untrusted/unchecked "counterparty" (here, the attacker's own resting order rather than a fake market) is allowed to participate in the same operation, and separate, unsynchronized state-update paths (in-memory object vs. direct store read/write) are reconciled incorrectly, so one path's write clobbers the other's — destroying value instead of duplicating it, but the same class of bug: dual/independent balance bookkeeping paths within a single transaction that are not properly serialized/re-read before the final commit.

### Impact Explanation
Any unprivileged account that places a resting sell order and later places a matching (self-trading) sell order on the opposite side can trigger a scenario where the maker-side credit committed via `addTrxOrToken(makerOrderCapsule, num)` is silently erased by the taker-side final `accountStore.put()`. This causes:
- Permanent loss of TRX or TRC10 asset balance that should have been credited to the account (the tokens sold by the maker order are debited/consumed from the order book, but the corresponding buy-side credit is lost when self-trade coincides on the same account), meeting the "permanent freezing/loss of funds" and "unbacked balance" criteria (the total supply accounted for in `AccountStore` no longer matches what should exist).
- Because market orders are placed via a plain signed `MarketSellAssetContract` transaction reachable by any account holder, this is fully exploitable by an unprivileged transaction broadcaster with no special permissions.

### Likelihood Explanation
Self-trading is common in real order books (arbitrage bots, market makers rebalancing against their own resting orders, or simply a user unaware they are about to match their own order) and nothing in `validate()` prevents it. The victim doesn't need to intend malicious behavior — the same code path can be deliberately triggered by an attacker who places two orders (a maker order, then a taker order that matches against it) to reliably reproduce the balance loss. The scenario only requires two ordinary market transactions from the same account, making it highly reachable and repeatable.

### Recommendation
- In `MarketSellAssetActuator.validate()` (or in `matchOrder`/`matchSingleOrder`), detect self-trade (taker `ownerAddress` == maker order `ownerAddress`) and either reject the order or process it as a single balance mutation on one canonical in-memory `AccountCapsule` instance.
- Refactor `addTrxOrToken(MarketOrderCapsule, long)` (the no-capsule overload) so that when the maker account equals the taker account, it reuses/updates the same `AccountCapsule` reference held by `execute()` rather than performing an independent `accountStore.get`/`put` cycle.
- More generally, avoid mixing "mutate in-memory, commit at the end" and "fetch-mutate-commit immediately" patterns for the same account within a single actuator execution; always re-fetch the latest capsule immediately before the final write, or centralize all balance mutations through one cached-and-committed-once object per account per transaction.

### Proof of Concept
1. Account `A` places order O1: sell 100 TOKEN_X for 100 TOKEN_Y (via `MarketSellAssetContract`), which rests in the order book (no match yet) — creates the resting maker order for `A`.
2. Account `A` (same address) places order O2: sell 100 TOKEN_Y for 100 TOKEN_X, which matches O1.
3. In `execute()` for O2, `accountCapsule` for `A` is loaded once at the top (reflecting balances after step 1, i.e., after `A`'s TOKEN_X was already debited to place O1).
4. `matchOrder`→`matchSingleOrder` runs: the taker (`A`, via O2) is credited TOKEN_X in the in-memory `takerAccountCapsule` (via `addTrxOrToken(takerOrderCapsule, ..., takerAccountCapsule)`), while the maker (`A`, via O1) is credited TOKEN_X's counterpart TOKEN_Y by `addTrxOrToken(makerOrderCapsule, num)`, which independently fetches `A`'s account from `accountStore`, adds the credit, and calls `accountStore.put()` immediately.
5. At the end of `execute()`, `accountStore.put(accountCapsule.createDbKey(), accountCapsule)` runs using the **taker-side** in-memory snapshot from step 3, which does not include the maker-side TOKEN_Y credit written in step 4 — overwriting the store and erasing that credit.
6. Result: account `A` ends up missing the TOKEN_Y (or TRX, if one side is TRX) that should have been credited from the maker fill, an amount permanently unaccounted for in `AccountStore`.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L113-116)
```java

      AccountCapsule accountCapsule = accountStore
          .get(contract.getOwnerAddress().toByteArray());

```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L136-148)
```java
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

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L164-281)
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

    // check order num
    MarketAccountOrderCapsule marketAccountOrderCapsule = marketAccountStore
        .getUnchecked(ownerAddress);
    if (marketAccountOrderCapsule != null
        && marketAccountOrderCapsule.getCount() >= MAX_ACTIVE_ORDER_NUM) {
      throw new ContractValidateException(
          "Maximum number of orders exceeded，" + MAX_ACTIVE_ORDER_NUM);
    }

    try {
      // Whether the balance is enough
      long fee = calcFee();

      if (Arrays.equals(sellTokenID, "_".getBytes())) {
        if (ownerAccount.getBalance() < addExact(sellTokenQuantity, fee)) {
          throw new ContractValidateException("No enough balance !");
        }
      } else {
        if (ownerAccount.getBalance() < fee) {
          throw new ContractValidateException("No enough balance !");
        }

        AssetIssueCapsule assetIssueCapsule = Commons
            .getAssetIssueStoreFinal(dynamicStore, assetIssueStore, assetIssueV2Store)
            .get(sellTokenID);
        if (assetIssueCapsule == null) {
          throw new ContractValidateException("No sellTokenId !");
        }
        if (!ownerAccount.assetBalanceEnoughV2(sellTokenID, sellTokenQuantity,
            dynamicStore)) {
          throw new ContractValidateException("SellToken balance is not enough !");
        }
      }

      if (!Arrays.equals(buyTokenID, "_".getBytes())) {
        // Whether have the token
        AssetIssueCapsule assetIssueCapsule = Commons
            .getAssetIssueStoreFinal(dynamicStore, assetIssueStore, assetIssueV2Store)
            .get(buyTokenID);
        if (assetIssueCapsule == null) {
          throw new ContractValidateException("No buyTokenId !");
        }
      }
    } catch (ArithmeticException e) {
      logger.debug(e.getMessage(), e);
      throw new ContractValidateException(e.getMessage());
    }

    return true;
  }
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
