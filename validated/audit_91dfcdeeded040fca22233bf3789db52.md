## Analog Found

### Title
Uncaught `ArithmeticException` in TRC10 order matching lets a malicious maker permanently DoS any taker's `MarketSellAssetContract` trade - ([File: actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java])

### Summary
The reported JOJO bug is a class of "forced-transfer-inside-a-shared-operation" DoS: a legitimate actor (a liquidator) tries to complete an operation that must push funds to a third party (the liquidated account), and that push can be made to fail by the third party's state (USDC blacklist), reverting the whole transaction and blocking the legitimate actor. The java-tron analog is in TRC10/TRX exchange order matching: filling a taker's `MarketSellAssetContract` forcibly credits tokens to the resting maker's account, and that credit can be made to throw an uncaught `ArithmeticException` if the maker has pre-loaded their own balance near `Long.MAX_VALUE`, which aborts the whole transaction outside the actuator's exception-handling contract.

### Finding Description
`MarketSellAssetActuator.matchSingleOrder()` matches a taker's order against a resting maker order and settles both sides by directly crediting balances/assets: [1](#0-0) 

The maker-side credit is performed by `addTrxOrToken(makerOrderCapsule, makerBuyTokenQuantityReceive)`, which loads the maker's account from the store and calls `addExact` on the *maker's current balance* — a value entirely controlled by the maker, not validated against overflow anywhere in `validate()`: [2](#0-1) 

Unlike `TransferAssetActuator`/`TransferContract`, which explicitly check for overflow in `validate()` and convert it to a `ContractValidateException` (`assetBalance = addExact(assetBalance, amount)` wrapped in try/catch), `MarketSellAssetActuator.execute()` does not catch `ArithmeticException` — its catch clause only covers `ItemNotFoundException | InvalidProtocolBufferException | BalanceInsufficientException | ContractValidateException`: [3](#0-2) 

`doValidate()` similarly only checks the *taker's* balance/asset sufficiency; it never checks whether crediting the resting maker order would overflow the maker's TRX balance or TRC10 asset balance: [4](#0-3) 

A maker can pre-position a huge TRX balance in their own account (TRX supply is bounded, but an attacker can still concentrate a very large amount, and for TRC10 assets the attacker can freely mint/hold up to `Long.MAX_VALUE` in a token they control, since `AccountCapsule.addAsset`/`addAssetAmountV2` overflow checks only fire when *this* transaction tries to add past `Long.MAX_VALUE`) then place a resting sell order. When an unrelated, unprivileged taker later broadcasts a `MarketSellAssetContract` that matches this poisoned order, `addExact` on the maker's balance throws an unchecked `ArithmeticException` from deep inside `matchSingleOrder`/`matchOrder`, which is not declared or caught by `execute()`. This propagates as a `RuntimeException` out of the actuator, outside the checked-exception contract (`ContractExeException`/`ContractValidateException`) that the surrounding transaction-processing code expects from actuators.

### Impact Explanation
This blocks a legitimate, unprivileged taker from executing a valid, well-formed `MarketSellAssetContract` transaction whenever it happens to match against a maliciously poisoned resting order — directly analogous to the JOJO liquidator being blocked from liquidating a valid position because of a third party's uncontrollable state. Because the exception type is unchecked and un-declared for this code path (unlike the analogous, correctly-guarded overflow check in `TransferAssetActuator.validate()`), it risks escaping the actuator's exception contract during block application rather than being converted into a normal failed-transaction result, which is a correctness/availability concern for transaction/block processing rather than a simple "transaction reverts" case. At minimum it is a reliable, repeatable denial-of-service against any taker whose order matches the poisoned maker order, permanently freezing that specific trade path.

### Likelihood Explanation
High: any account can freely accumulate up to `Long.MAX_VALUE` TRX or a self-issued TRC10 asset balance (subject to normal chain rules) and place a resting order via a single `MarketSellAssetContract`, with no privilege required. Any subsequent taker order that matches this price/pair will trigger the unguarded `addExact` overflow.

### Recommendation
Add the same overflow validation used in `TransferAssetActuator.validate()` (and `VMUtils.validateForSmartContract`) to `MarketSellAssetActuator.doValidate()`/`matchOrder()`: before crediting a maker's TRX balance or TRC10 asset amount during matching, verify with a checked `addExact`/`LongMath.checkedAdd` and convert any overflow into a `ContractValidateException` (rejecting the match, e.g. skipping/returning the order) rather than letting `ArithmeticException` propagate uncaught through `execute()`. Additionally, `execute()`'s catch list should be reviewed to ensure all exception types thrown transitively by matching logic are declared/handled, consistent with how `ArithmeticException` is already handled in `doValidate()`'s try/catch.

### Proof of Concept
1. Attacker account A funds itself (via normal transfers, or by minting a TRC10 asset it controls) so that its TRX balance or TRC10 asset balance for token X is close to `Long.MAX_VALUE`.
2. A places a `MarketSellAssetContract` selling asset Y for asset X at some price, creating a resting maker order that will credit A with asset X when matched (`addTrxOrToken(makerOrderCapsule, ...)` in `MarketSellAssetActuator.java:550-562`).
3. Any unrelated user B broadcasts a valid `MarketSellAssetContract` selling asset X for asset Y that matches A's resting order in `matchOrder`/`matchSingleOrder` (`MarketSellAssetActuator.java:307-499`).
4. During settlement, `addTrxOrToken(makerOrderCapsule, makerBuyTokenQuantityReceive)` calls `addExact(accountCapsule.getBalance(), num)` (or `addAssetAmountV2`) on A's near-max balance, throwing `ArithmeticException`, which is not in `execute()`'s catch clause (`MarketSellAssetActuator.java:152-159`) and propagates uncaught, causing B's legitimate transaction to fail/abort due to A's account state — the same "third-party forced-transfer DoS" root cause as the reported JOJO issue.

Note: I was unable to fully trace how an uncaught `RuntimeException`/`ArithmeticException` from within an actuator's `execute()` is ultimately handled by the block-application/transaction-processing layer (e.g., `Manager`/`TransactionTrace`) in this snapshot of the codebase, since those call sites were not retrievable via the available search tools before the iteration limit was reached. This should be verified directly (e.g., whether it is caught generically as a failed transaction or can crash/halt block processing) to fully confirm severity.

### Citations

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

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L241-278)
```java
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
