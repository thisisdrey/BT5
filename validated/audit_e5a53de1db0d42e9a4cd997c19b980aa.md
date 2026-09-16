### Title
Silent asset-balance corruption from ignored `reduceAssetAmountV2` return value in Exchange actuators - (File: `actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java`)

### Summary
`ExchangeInjectActuator.execute()` and the structurally identical `ExchangeTransactionActuator.execute()` call `AccountCapsule.reduceAssetAmountV2(...)` without checking its boolean return value, unlike sibling actuators (`TransferAssetActuator`, `ParticipateAssetIssueActuator`) which explicitly check the same method's return and throw `ContractExeException` on failure. This mirrors the reported Solidity bug class of ignoring an ERC20-style transfer's boolean success value.

### Finding Description
`reduceAssetAmountV2` returns `false` when the amount cannot be safely deducted (e.g. `currentAmount == null` or `amount > currentAmount`), and callers are expected to check it: [1](#0-0) 

`TransferAssetActuator` and `ParticipateAssetIssueActuator` correctly check this return value and abort the transaction on failure: [2](#0-1) [3](#0-2) 

`ExchangeInjectActuator.execute()`, however, calls `reduceAssetAmountV2` twice and ignores the return value in both places, while still unconditionally mutating the `ExchangeCapsule` balances (already computed) and persisting the account: [4](#0-3) 

The same pattern (ignored `reduceAssetAmountV2` return) exists in `ExchangeTransactionActuator.execute()`: [5](#0-4) 

### Impact Explanation
If `reduceAssetAmountV2` silently fails to deduct the seller's token balance (returns `false`) while the exchange pool balance (`exchangeCapsule.setBalance(...)`) and the "another token" credit (`addAssetAmountV2`) are still applied, the actuator commits `ret.setStatus(fee, code.SUCESS)` as if the transfer succeeded. This can create unbacked token balances in the exchange pool / counter-party account without the corresponding debit actually occurring, which is a form of "unbacked balance" / value-creation bug — matching the accepted impact categories (unbacked balance, unauthorized account operation).

### Likelihood Explanation
Under normal circumstances `doValidate()` for `ExchangeInjectActuator`/`ExchangeTransactionActuator` calls `assetBalanceEnoughV2` before `execute()`, which usually keeps `reduceAssetAmountV2` from failing. However, `validate()` and `execute()` re-fetch/re-derive account state independently and rely on `importAsset(key)` and map-key semantics inside `reduceAssetAmountV2`; because the failure path is completely silent (no exception, no rollback, no log), any divergence between the validated state and the executed state (e.g., asset-key resolution differences between `assetBalanceEnoughV2` and `reduceAssetAmountV2`, or an account state mutated between validate and execute for a given actuator instance) results in an inconsistent, unbacked balance with no error signaled anywhere. This is a genuine defensive-coding gap identical in class to the reported issue, but I could not fully confirm within this investigation whether a concrete state-mutation path exists in this codebase version that causes `assetBalanceEnoughV2` and `reduceAssetAmountV2` to disagree in practice (e.g., via multiple contracts in one transaction, or AllowSameTokenName toggling mid-processing) — that would require deeper tracing of `Manager`/`TransactionCapsule` batch contract execution which was not fully explored due to iteration limits.

### Recommendation
In `ExchangeInjectActuator.execute()` and `ExchangeTransactionActuator.execute()`, check the boolean return of `reduceAssetAmountV2` (and `addAssetAmountV2`) exactly as `TransferAssetActuator`/`ParticipateAssetIssueActuator` do, and throw `ContractExeException` on failure before applying any exchange-pool balance updates, ensuring atomicity between the debit and the pool/credit mutation.

### Proof of Concept
Not independently reproduced; the analog is derived by direct code comparison: `ExchangeInjectActuator`/`ExchangeTransactionActuator` unconditionally discard the boolean result of `reduceAssetAmountV2` at [6](#0-5) 
whereas `TransferAssetActuator` throws on the same failure condition at [7](#0-6) 
demonstrating the inconsistent/missing check.

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

**File:** actuator/src/main/java/org/tron/core/actuator/TransferAssetActuator.java (L75-79)
```java
      AccountCapsule ownerAccountCapsule = accountStore.get(ownerAddress);
      if (!ownerAccountCapsule
          .reduceAssetAmountV2(assetName.toByteArray(), amount, dynamicStore, assetIssueStore)) {
        throw new ContractExeException("reduceAssetAmount failed !");
      }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ParticipateAssetIssueActuator.java (L83-87)
```java
      AccountCapsule toAccount = accountStore.get(toAddress);
      toAccount.setBalance(addExact(toAccount.getBalance(), cost));
      if (!toAccount.reduceAssetAmountV2(key, exchangeAmount, dynamicStore, assetIssueStore)) {
        throw new ContractExeException("reduceAssetAmount failed !");
      }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L85-106)
```java
      long newBalance = subtractExact(accountCapsule.getBalance(), calcFee());
      accountCapsule.setBalance(newBalance);

      if (Arrays.equals(tokenID, TRX_SYMBOL_BYTES)) {
        accountCapsule.setBalance(subtractExact(newBalance, tokenQuant));
      } else {
        accountCapsule.reduceAssetAmountV2(tokenID, tokenQuant, dynamicStore, assetIssueStore);
      }

      if (Arrays.equals(anotherTokenID, TRX_SYMBOL_BYTES)) {
        accountCapsule.setBalance(subtractExact(newBalance, anotherTokenQuant));
      } else {
        accountCapsule
            .reduceAssetAmountV2(anotherTokenID, anotherTokenQuant, dynamicStore, assetIssueStore);
      }
      accountStore.put(accountCapsule.createDbKey(), accountCapsule);

      Commons.putExchangeCapsule(exchangeCapsule, dynamicStore, exchangeStore, exchangeV2Store,
          assetIssueStore);

      ret.setExchangeInjectAnotherAmount(anotherTokenQuant);
      ret.setStatus(fee, code.SUCESS);
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L77-99)
```java
      long newBalance = subtractExact(accountCapsule.getBalance(), calcFee());
      accountCapsule.setBalance(newBalance);

      if (Arrays.equals(tokenID, TRX_SYMBOL_BYTES)) {
        accountCapsule.setBalance(subtractExact(newBalance, tokenQuant));
      } else {
        accountCapsule.reduceAssetAmountV2(tokenID, tokenQuant, dynamicStore, assetIssueStore);
      }

      if (Arrays.equals(anotherTokenID, TRX_SYMBOL_BYTES)) {
        accountCapsule.setBalance(addExact(newBalance, anotherTokenQuant));
      } else {
        accountCapsule
            .addAssetAmountV2(anotherTokenID, anotherTokenQuant, dynamicStore, assetIssueStore);
      }

      accountStore.put(accountCapsule.createDbKey(), accountCapsule);

      Commons.putExchangeCapsule(exchangeCapsule, dynamicStore, exchangeStore, exchangeV2Store,
          assetIssueStore);

      ret.setExchangeReceivedAmount(anotherTokenQuant);
      ret.setStatus(fee, code.SUCESS);
```
