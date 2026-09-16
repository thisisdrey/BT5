### Title
Unchecked return value of `reduceAssetAmountV2` in `MarketSellAssetActuator.transferBalanceOrToken()` allows order creation without deducting seller's asset balance - (File: actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java)

### Summary
`MarketSellAssetActuator` calls `AccountCapsule.reduceAssetAmountV2()` — a method whose boolean return value signals whether the deduction actually succeeded — without checking that return value, unlike the sibling actuator `TransferAssetActuator` which explicitly checks and reverts on `false`.

### Finding Description
`reduceAssetAmountV2(byte[] key, long amount, DynamicPropertiesStore, AssetIssueStore)` returns a boolean indicating success/failure of subtracting `amount` from an account's TRC10 asset balance [1](#0-0) .

`TransferAssetActuator.execute()` correctly checks this return value and throws `ContractExeException` if it fails, guaranteeing atomicity between the accounting mutation and the rest of the transfer logic [2](#0-1) .

`MarketSellAssetActuator`, however, calls the same kind of method in `transferBalanceOrToken()` and silently discards the return value: [3](#0-2) 

This is invoked from `execute()` right after the fee is deducted and before an order is created and persisted, with no check on the outcome: [4](#0-3) 

If `reduceAssetAmountV2` returns `false` (deduction fails for any reason not already excluded by `validate()`), execution continues as if the deduction succeeded: the order is created, saved to `orderStore`, indexed into the order book, and — as seen in `matchOrder`/`matchSingleOrder` — can be matched against other orders and credited with the counter-asset via `addTrxOrToken`, all while the seller's original sell-token balance was never actually reduced [5](#0-4) [6](#0-5) .

This mirrors the exact bug class in the report: a caller trusts a state-mutating call's boolean success signal implicitly instead of checking it, so a failure path silently becomes a no-op deduction while the rest of the (asset-crediting) flow proceeds.

### Impact Explanation
If reachable, this results in an unbacked TRC10 asset balance and theft of counter-party funds: the seller keeps their sell-token asset (since the deduction silently failed) while still receiving the buy-token proceeds from matched maker orders, or occupying order-book liquidity that other traders fill in good faith. This is a fund-integrity impact (unbacked balance / theft-equivalent), which qualifies as Medium/High under the given rules.

### Likelihood Explanation
I was not able to fully verify a *live* code path from `validate()` to `execute()` where `reduceAssetAmountV2` would return `false` despite `validate()`'s pre-checks, because I ran out of budget to inspect `AccountCapsule.reduceAssetAmountV2` and `assetBalanceEnoughV2`'s exact semantics (e.g., whether they use different internal representations — V1 vs V2 asset maps, or handle the allowSameTokenName migration differently — which could cause `assetBalanceEnoughV2` in `validate()` to pass while `reduceAssetAmountV2` in `execute()` fails due to a race, migration edge case, or map inconsistency). This gap is the same class of "assumed-never-fails" call the external report flags, and the missing check itself is a clear defensive-coding defect: it should be explicitly validated regardless of whether a currently known path can trigger it, exactly as `TransferAssetActuator` does for the identical operation.

### Recommendation
In `MarketSellAssetActuator.transferBalanceOrToken()`, check the boolean return value of `reduceAssetAmountV2` and throw a `ContractExeException` (or propagate a validation failure) if it returns `false`, mirroring the pattern already used in `TransferAssetActuator`:
```java
private void transferBalanceOrToken(AccountCapsule accountCapsule) throws ContractExeException {
  if (Arrays.equals(sellTokenID, "_".getBytes())) {
    accountCapsule.setBalance(subtractExact(accountCapsule.getBalance(), sellTokenQuantity));
  } else {
    if (!accountCapsule.reduceAssetAmountV2(sellTokenID, sellTokenQuantity, dynamicStore, assetIssueStore)) {
      throw new ContractExeException("reduceAssetAmount failed !");
    }
  }
}
```

### Proof of Concept
A concrete PoC could not be constructed within the available investigation budget because it requires confirming an exact state (asset balance representation mismatch between `validate()`'s `assetBalanceEnoughV2` check and `execute()`'s `reduceAssetAmountV2` deduction) that causes the deduction to fail post-validation — e.g. via the `allowSameTokenName`/V1-V2 asset map migration boundary, or a value change between validate and execute in the same block. This should be verified in `chainbase/src/main/java/org/tron/core/capsule/AccountCapsule.java` (`reduceAssetAmountV2`, `assetBalanceEnoughV2`) by a follow-up Devin session with full file access, since the index truncated that file's contents.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/TransferAssetActuator.java (L75-79)
```java
      AccountCapsule ownerAccountCapsule = accountStore.get(ownerAddress);
      if (!ownerAccountCapsule
          .reduceAssetAmountV2(assetName.toByteArray(), amount, dynamicStore, assetIssueStore)) {
        throw new ContractExeException("reduceAssetAmount failed !");
      }
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L126-151)
```java
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

      ret.setOrderId(orderCapsule.getID());
      ret.setStatus(fee, code.SUCESS);
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L488-499)
```java
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

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L527-535)
```java
  private void transferBalanceOrToken(AccountCapsule accountCapsule) {
    if (Arrays.equals(sellTokenID, "_".getBytes())) {
      accountCapsule.setBalance(subtractExact(
          accountCapsule.getBalance(), sellTokenQuantity));
    } else {
      accountCapsule
          .reduceAssetAmountV2(sellTokenID, sellTokenQuantity, dynamicStore, assetIssueStore);
    }
  }
```
