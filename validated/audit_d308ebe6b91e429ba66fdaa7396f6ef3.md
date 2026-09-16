### Title
Griefable market-order matching cap allows cheap DoS of `MarketSellAssetContract` trading on a token pair - (File: `actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java`)

### Summary
`MarketSellAssetActuator` enforces a hard cap of `MAX_MATCH_NUM = 20` maker orders that a single taker order may cross in one transaction [1](#0-0) , but this cap is checked only deep inside `matchOrder()`/`execute()`, never during `validate()` [2](#0-1) . `validate()` only checks the caller's own open-order count against `MAX_ACTIVE_ORDER_NUM = 100` [3](#0-2) , with no restriction on how many *other* accounts can place tiny maker orders at the same best price for a given token pair. This mirrors the reported bug class: a cheap, unprivileged transaction can permanently manipulate shared on-chain state (the order book at a specific price level) to make a subsequent, legitimate user's otherwise-valid transaction revert deterministically, causing a scalable, low-cost DoS.

### Finding Description
When a `MarketSellAssetContract` is executed, the actuator:
1. Deducts the market-sell fee and transfers the sell-side asset out of the taker's account [4](#0-3) .
2. Creates and persists the taker order [5](#0-4) .
3. Calls `matchOrder()`, which walks the maker order list at the best price for the pair and matches against successive maker orders one at a time until the taker's remaining quantity is filled [6](#0-5) .
4. Inside that loop, every matched maker order increments `matchOrderCount`, and once it exceeds `MAX_MATCH_NUM` (20) the actuator throws `ContractValidateException("Too many matches. MAX_MATCH_NUM = " + MAX_MATCH_NUM)` [7](#0-6) .

Because this throw happens inside `execute()`, not `validate()`, it is only caught by the generic catch block in `execute()` which converts it to `ContractExeException` and marks the transaction `FAILED` [8](#0-7) . Any attacker who is willing to pay `MarketSellFee` roughly 21+ times can post 21+ tiny maker orders (minimum-quantity, same best price) for a token pair using `MarketSellAssetContract` itself — this contract type is fully permissionless and reachable by any signed transaction, with no minimum quantity floor beyond `> 0` [9](#0-8) . Once in place, every future taker who tries to sell into that price level with an order that would cross more than 20 of these fragments deterministically fails with "Too many matches", even though nothing is wrong with the taker's own transaction. Because there is no validate-time check or protection preventing an attacker from replenishing the fragments as fast as they are consumed (each maker fragment consumed just needs one more tiny order re-posted), the DoS can be sustained indefinitely at low cost — the same "small deposit/withdraw front-running to flip shared state and break other users' transactions" pattern described in the reference report, just expressed against the order-matching path instead of a `Status` enum.

### Impact Explanation
This allows an unprivileged, low-cost attacker to durably deny service to the on-chain TRC10 market (`MarketSellAssetContract`) for any token pair they choose, in perpetuity, for the cost of a handful of small orders plus the market-sell fee per order. Legitimate takers' transactions fail and burn bandwidth/energy/fees for nothing, and no economically-rational trader can cross that price level. This is a concrete, node-reachable market disruption/DoS of a core exchange feature that any account can trigger via a single, ordinary signed transaction type — no special privilege, contract deployment, or off-chain access required.

### Likelihood Explanation
High. `MarketSellAssetContract` is a standard, permissionless actuator (gated only by `supportAllowMarketTransaction()`, i.e., enabled once by governance) [10](#0-9) . The `MAX_MATCH_NUM` constant is fixed (20) and the account-level order cap (`MAX_ACTIVE_ORDER_NUM` = 100) does not prevent a single attacker from repeatedly posting and reposting >20 tiny orders at the best price, nor does it prevent using multiple attacker-controlled accounts to bypass any per-account limit entirely. No mempool front-running is even required — the attacker can simply pre-seed the order book for a pair before anyone attempts a larger crossing trade.

### Recommendation
Move the `MAX_MATCH_NUM` bound into `validate()` (e.g., by pre-simulating/estimating the number of price-level crossings needed, or by capping/aggregating matches within a price level rather than counting per maker order) so the failure is deterministic and rejected up front rather than reachable only via a state-dependent execute()-time revert. Alternatively, allow partial fills up to `MAX_MATCH_NUM` matches per transaction (returning the unfilled remainder to the taker rather than reverting the whole transaction), and/or impose an economically meaningful minimum order size relative to the trading pair to make spamming a price level to 20+ maker fragments economically costly rather than trivial.

### Proof of Concept
1. Governance enables `AllowMarketTransaction`.
2. Attacker account A repeatedly submits `MarketSellAssetContract` transactions selling TokenX for TokenY at the current best price, each with the minimum valid quantity (`sellTokenQuantity = 1`, satisfying only the `> 0` check) [9](#0-8) , until 21+ resting maker orders exist at that exact price for the pair (paying `MarketSellFee` per order, and staying under `MAX_ACTIVE_ORDER_NUM=100` per account, or spreading across a few accounts).
3. Victim account B submits a legitimate `MarketSellAssetContract` transaction to sell into the opposite side of this pair with a quantity large enough that it would need to cross more than 20 of A's resting orders to fill.
4. `matchOrder()` iterates A's fragments; once `matchOrderCount` exceeds `MAX_MATCH_NUM`, it throws `ContractValidateException("Too many matches. MAX_MATCH_NUM = 20")` [7](#0-6) , which propagates out of `execute()` as `ContractExeException`, and B's transaction fails [8](#0-7) .
5. B (and every subsequent taker attempting to cross that price level with similar size) is permanently unable to trade against this price level as long as A maintains ≥21 fragments there, which A can do indefinitely by reposting orders as they are consumed, at a cost dramatically lower than the value denied to victims.

*Note: I was unable to fully verify within the available context whether the store/session changes made earlier in `execute()` (fee deduction, asset transfer out of the taker, order persistence) are rolled back by the surrounding transaction-processing/revoking-session machinery in `Manager`/`TransactionTrace` when `ContractExeException` propagates, since I ran out of tool calls before confirming this in `TransactionTrace.java`. This does not affect the core DoS finding (the taker's trade fails and cannot execute), but it would affect whether there is an additional, secondary impact (partial state corruption or fee loss) beyond the denial of service itself — this should be verified by a follow-up review of `TransactionTrace.exec()`/`Manager.processTransaction()`.*

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L62-66)
```java
  @Getter
  @Setter
  private static int MAX_ACTIVE_ORDER_NUM = 100;
  @Getter
  private static int MAX_MATCH_NUM = 20;
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L125-134)
```java
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
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L136-137)
```java
      // 2. create and save order
      MarketOrderCapsule orderCapsule = createAndSaveOrder(accountCapsule, contract);
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L152-159)
```java
    } catch (ItemNotFoundException
        | InvalidProtocolBufferException
        | BalanceInsufficientException
        | ContractValidateException e) {
      logger.debug(e.getMessage(), e);
      ret.setStatus(fee, code.FAILED);
      throw new ContractExeException(e.getMessage());
    }
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

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L307-360)
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

        // remove order
        if (makerOrderCapsule.getSellTokenQuantityRemain() == 0) {
          // remove from market order list
          orderIdListCapsule.removeOrder(makerOrderCapsule, orderStore,
              pairPriceKey, pairPriceToOrderStore);
        }

        matchOrderCount++;
        if (matchOrderCount > MAX_MATCH_NUM) {
          throw new ContractValidateException("Too many matches. MAX_MATCH_NUM = " + MAX_MATCH_NUM);
        }
      }
```
