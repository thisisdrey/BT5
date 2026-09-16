### Title
Unchecked return value of `reduceAssetAmountV2` in Market Order execution can mint unbacked TRC10 tokens to the counterparty - (File: `actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java`)

### Summary
`MarketSellAssetActuator.transferBalanceOrToken()` deducts the seller's `sellTokenID` balance by calling `AccountCapsule.reduceAssetAmountV2(...)` but never checks its boolean return value, unlike every other actuator in the codebase that performs the same kind of asset-balance debit.

### Finding Description
`reduceAssetAmountV2` in `AccountCapsule` is a "soft-fail" operation: it does not throw on insufficient/mismatched balance, it silently returns `false` and leaves the account state untouched: [1](#0-0) 

Every other actuator that relies on this method treats its return value as authoritative and aborts the transaction if the deduction did not happen:
- `TransferAssetActuator.execute()` throws `ContractExeException("reduceAssetAmount failed !")` if the call returns `false`: [2](#0-1) 
- `ParticipateAssetIssueActuator.execute()` does the same check on `toAccount.reduceAssetAmountV2(...)`: [3](#0-2) 

`MarketSellAssetActuator.transferBalanceOrToken()`, however, calls the same method and discards the result: [4](#0-3) 

This call happens inside `execute()` right before the order is created and matched: [5](#0-4) 

Crucially, once the (unchecked) debit step runs, `matchOrder()`/`matchSingleOrder()` unconditionally credits the opposite party (the resting "maker" order owner) with the seller's token via `addTrxOrToken(makerOrderCapsule, makerBuyTokenQuantityReceive)`, which internally calls `accountCapsule.addAssetAmountV2(buyTokenId, num, ...)`: [6](#0-5) 

If `reduceAssetAmountV2` in `transferBalanceOrToken()` returns `false` for any reason (e.g. a key/token-id resolution mismatch between the `assetBalanceEnoughV2` check performed in `validate()` and the `reduceAssetAmountV2` debit performed in `execute()` — both of which branch on `dynamicPropertiesStore.getAllowSameTokenName()` and on separate `AssetIssueStore`/`AssetIssueV2Store` lookups), the seller's balance is left unchanged while the order proceeds to be matched and the maker (or the seller itself, in the reflexive add path) is credited with tokens that were never actually removed from any account. This is the exact bug class from the report: a value-transfer call whose failure (`false`/no-op) is not checked, so the higher-level operation (asset transfer / order settlement) completes as if payment had been made.

### Impact Explanation
If the silent-failure path is triggered, TRC10 token supply becomes unbacked: the maker/counterparty's account balance for a token is increased via `addAssetAmountV2` without a corresponding, verified decrease anywhere in the ledger, because the actuator does not abort or roll back when the debit silently no-ops. This is a direct "unbacked balance / theft of funds" condition reachable by any unprivileged account broadcasting a `MarketSellAssetContract` transaction, matching the `MarketSellAssetActuator` code path explicitly listed as in-scope ("exchange and market order handling").

### Likelihood Explanation
Reaching this requires a state where `reduceAssetAmountV2` returns `false` despite `validate()`'s `assetBalanceEnoughV2` having passed for the same token/quantity. Under normal, uninterrupted sequential execution of `validate()` then `execute()` for one transaction, both checks read the same account snapshot and should agree, so this is not trivially triggerable on every call — it depends on divergent key/token-id resolution between the V1 (`AssetIssueStore`) and V2 (`AssetIssueV2Store`) asset lookups used by `assetBalanceEnoughV2` versus `reduceAssetAmountV2`, or on the same-token-name migration flag changing between checks. I was not able to fully construct and verify a concrete transaction sequence within the available tool budget that forces `reduceAssetAmountV2` to fail after `assetBalanceEnoughV2` succeeds; this should be validated with targeted testing (e.g. toggling `AllowSameTokenName`, or crafting `sellTokenID` values that resolve to different keys in the two lookups) before treating exploitability as fully confirmed.

### Recommendation
Make `transferBalanceOrToken()` check the boolean result of `reduceAssetAmountV2` exactly as `TransferAssetActuator` and `ParticipateAssetIssueActuator` do, and abort/throw `ContractExeException` on failure so that no order is created or matched, and no counterparty balance is credited, when the seller's debit does not actually occur. More broadly, consider making `reduceAssetAmountV2`/`addAssetAmountV2` throw rather than return a silently-ignorable boolean, to prevent this unchecked-return-value pattern from recurring in future actuators.

### Proof of Concept
Not fully constructed — a concrete PoC requires identifying an input to `MarketSellAssetContract` (`sellTokenId`) for which `AccountCapsule.assetBalanceEnoughV2()` (used in `validate()`) and `AccountCapsule.reduceAssetAmountV2()` (used in `execute()`) resolve to different underlying asset-map keys or amounts (e.g. through the `AllowSameTokenName`-gated V1/V2 key resolution difference documented in `AccountCapsule.java` lines 701-813), so that validation passes but the debit silently no-ops while `matchOrder()` still credits the counterparty.

### Citations

**File:** chainbase/src/main/java/org/tron/core/capsule/AccountCapsule.java (L780-813)
```java
  public boolean reduceAssetAmountV2(byte[] key, long amount,
                                     DynamicPropertiesStore dynamicPropertiesStore, AssetIssueStore assetIssueStore) {
    importAsset(key);
    //key is token name
    boolean disableJavaLangMath = dynamicPropertiesStore.disableJavaLangMath();
    if (dynamicPropertiesStore.getAllowSameTokenName() == 0) {
      Map<String, Long> assetMap = this.account.getAssetMap();
      AssetIssueCapsule assetIssueCapsule = assetIssueStore.get(key);
      String tokenID = assetIssueCapsule.getId();
      String nameKey = ByteArray.toStr(key);
      Long currentAmount = assetMap.get(nameKey);
      if (amount > 0 && null != currentAmount && amount <= currentAmount) {
        this.account = this.account.toBuilder()
                .putAsset(nameKey, subtractExact(currentAmount, amount, disableJavaLangMath))
                .putAssetV2(tokenID, subtractExact(currentAmount, amount, disableJavaLangMath))
                .build();
        return true;
      }
    }
    //key is token id
    if (dynamicPropertiesStore.getAllowSameTokenName() == 1) {
      String tokenID = ByteArray.toStr(key);
      Map<String, Long> assetMapV2 = this.account.getAssetV2Map();
      Long currentAmount = assetMapV2.get(tokenID);
      if (amount > 0 && null != currentAmount && amount <= currentAmount) {
        this.account = this.account.toBuilder()
                .putAssetV2(tokenID, subtractExact(currentAmount, amount, disableJavaLangMath))
                .build();
        return true;
      }
    }

    return false;
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/TransferAssetActuator.java (L75-80)
```java
      AccountCapsule ownerAccountCapsule = accountStore.get(ownerAddress);
      if (!ownerAccountCapsule
          .reduceAssetAmountV2(assetName.toByteArray(), amount, dynamicStore, assetIssueStore)) {
        throw new ContractExeException("reduceAssetAmount failed !");
      }
      accountStore.put(ownerAddress, ownerAccountCapsule);
```

**File:** actuator/src/main/java/org/tron/core/actuator/ParticipateAssetIssueActuator.java (L82-87)
```java
      byte[] toAddress = participateAssetIssueContract.getToAddress().toByteArray();
      AccountCapsule toAccount = accountStore.get(toAddress);
      toAccount.setBalance(addExact(toAccount.getBalance(), cost));
      if (!toAccount.reduceAssetAmountV2(key, exchangeAmount, dynamicStore, assetIssueStore)) {
        throw new ContractExeException("reduceAssetAmount failed !");
      }
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L133-151)
```java
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
