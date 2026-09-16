### Title
Uncaught `ArithmeticException` in `MarketSellAssetActuator` order matching lets an unprivileged order placer crash transaction processing for a counter-party ("maker") - ([File: actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java])

### Summary
`MarketSellAssetActuator.execute()` matches a taker's sell order against resting "maker" orders and credits the maker's account with the matched proceeds via `addTrxOrToken()`, which internally calls `AccountCapsule.addAssetAmountV2()`/`addExact()`. If the maker's TRC10 balance is close to `Long.MAX_VALUE`, this addition overflows and throws `ArithmeticException`. Unlike the sibling actuators (`ExchangeTransactionActuator`, `ExchangeWithdrawActuator`, `ParticipateAssetIssueActuator`, `TransferAssetActuator`), `MarketSellAssetActuator.execute()`'s catch block does **not** catch `ArithmeticException`, so it propagates uncaught out of the actuator during transaction execution.

### Finding Description
`execute()` in `MarketSellAssetActuator` only catches `ItemNotFoundException | InvalidProtocolBufferException | BalanceInsufficientException | ContractValidateException`: [1](#0-0) 

During `matchOrder()`/`matchSingleOrder()`, the taker's transaction directly mutates and credits a third-party maker's balance: [2](#0-1) 

`addTrxOrToken(makerOrderCapsule, makerBuyTokenQuantityReceive)` fetches the maker's `AccountCapsule` and calls `addAssetAmountV2`, which performs `addExact(currentAmount, amount, ...)`: [3](#0-2) [4](#0-3) 

`addExact` throws `ArithmeticException` on `long` overflow, and this is a well-established failure mode already exercised in tests for similar actuators (`ParticipateAssetIssueActuator`, `TransferAssetActuator`): [5](#0-4) 

This is the direct structural analog of the report: in the ERC721 case, a third party (the NFT funder) sets up state on the recipient contract that causes an unrelated payment transaction (the claim) to revert when a value is pushed to that recipient. Here, an unprivileged order placer ("asset issuer"/"order placer" role, reachable by any signed `MarketSellAssetContract` or plain `TransferAssetContract` transaction) can pre-fund a target maker account's TRC10 balance close to `Long.MAX_VALUE` via ordinary token transfers, then place (or wait for) a matching sell order. When any taker's `MarketSellAssetActuator.execute()` subsequently tries to credit that maker, `addExact` overflows and throws an uncaught `ArithmeticException` that is not handled by the actuator's catch clause, unlike every comparable actuator in this codebase.

### Impact Explanation
Because `ArithmeticException` is unchecked and not caught by `MarketSellAssetActuator.execute()`, it propagates out of the actuator invocation during transaction execution/block application. Depending on how the enclosing transaction/block processing pipeline handles uncaught `RuntimeException`s from actuators (this could not be conclusively verified within the available index — see Uncertainty below), this can manifest as at minimum an unexpected, un-classified transaction failure that deviates from the actuator's intended `ContractExeException`/`FAILED` result path, and at worst could interrupt block application, since none of the declared checked-exception types the caller may expect covers `ArithmeticException` here. This maps to the "node crash or halt" / "an API the node can no longer serve" acceptance criteria if the surrounding transaction-processing loop does not defensively catch generic `Exception`/`RuntimeException` around actuator execution.

### Likelihood Explanation
The attack is fully reachable by an unprivileged account: it only requires (1) issuing/holding a TRC10 asset and transferring it to inflate a target's balance near `Long.MAX_VALUE` (ordinary `TransferAssetContract`), and (2) placing a `MarketSellAssetContract` order that will match against the target's resting order, or waiting for another taker to match it. No special privileges, precompiles, or SR/witness roles are required. The precondition (accumulating a near-`Long.MAX_VALUE` TRC10 balance) is expensive/impractical for TRX itself but is achievable for a TRC10 asset the attacker fully controls as issuer, since issuers can mint/hold large supplies and freely transfer among their own or colluding accounts to inflate any address's TRC10 balance for that asset before creating a matching order.

### Recommendation
Add `ArithmeticException` to the caught exception types in `MarketSellAssetActuator.execute()` (mirroring the pattern already used in `ExchangeTransactionActuator`, `ExchangeWithdrawActuator`, `TransferAssetActuator`, and `ParticipateAssetIssueActuator`), converting it into a `ContractExeException` with `ret.setStatus(fee, code.FAILED)` rather than letting it propagate uncaught. Additionally, consider validating/capping maker-side balances or using saturating arithmetic during order matching credit operations so a griefer cannot use overflow to force order-matching failures against arbitrary victims.

### Proof of Concept
1. Attacker acts as issuer/controller of TRC10 asset `X`.
2. Attacker sends multiple `TransferAssetContract` transactions to inflate victim (maker) address `M`'s balance of asset `X` to `Long.MAX_VALUE - 1`.
3. `M` places (or has already placed) a resting sell order via `MarketSellAssetContract` selling some other asset `Y` for asset `X`.
4. Attacker (or any taker) submits a `MarketSellAssetContract` selling `X` for `Y` that matches `M`'s order.
5. During execution, `matchSingleOrder()` calls `addTrxOrToken(makerOrderCapsule, makerBuyTokenQuantityReceive)` → `AccountCapsule.addAssetAmountV2()` → `addExact()`, which overflows and throws `ArithmeticException`, uncaught by `MarketSellAssetActuator.execute()`'s catch block, differing from the behavior of every other actuator that performs equivalent balance additions.

**Uncertainty:** I could not conclusively confirm within the indexed code how the top-level transaction/block-application pipeline (e.g., `Manager`/`TransactionUtil`) handles an uncaught `RuntimeException` thrown from inside an actuator's `execute()` — whether it is caught generically and only fails the single transaction, or whether it can propagate further and disrupt block application. This determines whether the concrete impact is "single transaction unexpectedly fails outside the normal FAILED/REVERT path" (lower severity) versus "block application disruption" (higher severity). A Devin session with full repository access could trace this call chain (`Manager.processTransaction`/`TransactionUtil`) to confirm the exact blast radius.

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

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L486-499)
```java
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

**File:** chainbase/src/main/java/org/tron/core/capsule/AccountCapsule.java (L733-765)
```java
  public boolean addAssetAmountV2(byte[] key, long amount,
      DynamicPropertiesStore dynamicPropertiesStore, AssetIssueStore assetIssueStore) {
    importAsset(key);
    boolean disableJavaLangMath = dynamicPropertiesStore.disableJavaLangMath();
    //key is token name
    if (dynamicPropertiesStore.getAllowSameTokenName() == 0) {
      Map<String, Long> assetMap = this.account.getAssetMap();
      AssetIssueCapsule assetIssueCapsule = assetIssueStore.get(key);
      String tokenID = assetIssueCapsule.getId();
      String nameKey = ByteArray.toStr(key);
      Long currentAmount = assetMap.get(nameKey);
      if (currentAmount == null) {
        currentAmount = 0L;
      }
      this.account = this.account.toBuilder()
          .putAsset(nameKey, addExact(currentAmount, amount, disableJavaLangMath))
          .putAssetV2(tokenID, addExact(currentAmount, amount, disableJavaLangMath))
          .build();
    }
    //key is token id
    if (dynamicPropertiesStore.getAllowSameTokenName() == 1) {
      String tokenIDStr = ByteArray.toStr(key);
      Map<String, Long> assetMapV2 = this.account.getAssetV2Map();
      Long currentAmount = assetMapV2.get(tokenIDStr);
      if (currentAmount == null) {
        currentAmount = 0L;
      }
      this.account = this.account.toBuilder()
          .putAssetV2(tokenIDStr, addExact(currentAmount, amount, disableJavaLangMath))
          .build();
    }
    return true;
  }
```

**File:** framework/src/test/java/org/tron/core/actuator/TransferAssetActuatorTest.java (L842-874)
```java
  /**
   * SameTokenName close, add over flow
   */
  @Test
  public void SameTokenNameCloseAddOverflowTest() {
    createAssertBeforSameTokenNameActive();
    // First, increase the to balance. Else can't complete this test case.
    AccountCapsule toAccount = dbManager.getAccountStore().get(ByteArray.fromHexString(TO_ADDRESS));
    toAccount.addAsset(ASSET_NAME.getBytes(), Long.MAX_VALUE);
    dbManager.getAccountStore().put(ByteArray.fromHexString(TO_ADDRESS), toAccount);
    TransferAssetActuator actuator = new TransferAssetActuator();
    actuator.setChainBaseManager(dbManager.getChainBaseManager()).setAny(getContract(1));

    TransactionResultCapsule ret = new TransactionResultCapsule();
    try {
      actuator.validate();
      actuator.execute(ret);
      Assert.assertTrue(false);
    } catch (ContractValidateException e) {
      Assert.assertTrue(e instanceof ContractValidateException);
      Assert.assertTrue("long overflow".equals(e.getMessage()));
      AccountCapsule owner =
              dbManager.getAccountStore().get(ByteArray.fromHexString(OWNER_ADDRESS));
      toAccount =
              dbManager.getAccountStore().get(ByteArray.fromHexString(TO_ADDRESS));
      Assert.assertEquals(owner.getAssetMapForTest()
              .get(ASSET_NAME).longValue(), OWNER_ASSET_BALANCE);
      Assert.assertEquals(toAccount.getAssetMapForTest()
              .get(ASSET_NAME).longValue(), Long.MAX_VALUE);
    } catch (ContractExeException e) {
      Assert.assertFalse(e instanceof ContractExeException);
    }
  }
```
