### Title
Unchecked return value of `reduceAssetAmountV2` in `ExchangeInjectActuator.execute` allows exchange pool balance to be inflated without a matching asset deduction - (File: `actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java`)

### Summary
`ExchangeInjectActuator.execute()` calls `AccountCapsule.reduceAssetAmountV2(...)` twice to debit the injecting account's TRC10 asset balances, but ignores the boolean success/failure return value of that method, while it unconditionally credits the exchange pair's pool balances (`exchangeCapsule.setBalance(...)`) and marks the transaction `SUCESS`.

### Finding Description
`reduceAssetAmountV2` only mutates the account's asset map and returns `true` when `amount > 0 && currentAmount != null && amount <= currentAmount`; otherwise it leaves the account untouched and returns `false`: [1](#0-0) 

In `ExchangeInjectActuator.execute()`, the exchange's pool balances are updated first via `exchangeCapsule.setBalance(...)`, and then the two calls that are supposed to actually debit the user's asset ignore this return value entirely: [2](#0-1) 

Compare this to the sibling actuators that perform the exact same kind of operation and correctly check the return value, throwing `ContractExeException` on failure so the transaction is not silently marked successful: [3](#0-2) [4](#0-3) [5](#0-4) 

This is the same bug class as the reported `ClaimAssessor.requestClaim` issue: a state-mutating sub-call's success/failure status is discarded, so the caller cannot detect and react to a failed operation, and proceeds as if it succeeded.

`ExchangeWithdrawActuator` has the mirrored, equally unchecked pattern for `addAssetAmountV2` (crediting the user while decrementing pool balances), compounding the risk on both sides of the exchange: [6](#0-5) 

### Impact Explanation
If `reduceAssetAmountV2` ever returns `false` at execute time (asset amount insufficient or absent for the exact key used), the account is not debited, yet:
- the exchange's `firstTokenBalance`/`secondTokenBalance` have already been incremented by the full injected amount, and
- the actuator still calls `ret.setStatus(fee, code.SUCESS)` and persists the exchange capsule unchanged for this discrepancy.

This desynchronizes the on-chain exchange pool accounting from actual backing asset holdings — the pool believes it holds more of a token than users have actually contributed. Any subsequent `ExchangeTransactionActuator` trade or `ExchangeWithdrawActuator` withdrawal against this inflated pool balance can pay out tokens that were never actually deposited, which is an unbacked-balance / asset-inflation condition reachable purely through ordinary `ExchangeInjectContract` broadcasts by the exchange creator.

### Likelihood Explanation
`ExchangeInjectActuator.validate()` does call `assetBalanceEnoughV2` before execution, which is intended to make the `reduceAssetAmountV2` calls in `execute()` always succeed under normal conditions: [7](#0-6) 

I could not find and verify a concrete state-divergence path (e.g., a difference between the key/lookup used by `assetBalanceEnoughV2` at validate-time versus `reduceAssetAmountV2` at execute-time, or a mid-transaction mutation of the same `AccountCapsule`/`AssetIssueStore` state) that would make the two checks disagree within a single actuator invocation. This is a genuine defense-in-depth gap and a real deviation from the pattern used by `TransferAssetActuator`/`ParticipateAssetIssueActuator`/`Commons.adjustAssetBalanceV2`, but I cannot confirm from static review alone that it is independently, currently exploitable without such a validate/execute divergence — that would need dynamic testing or a Devin session with full repo/test access to construct a proof of concept.

### Recommendation
Check the boolean return values of `reduceAssetAmountV2` (in `ExchangeInjectActuator`) and `addAssetAmountV2` (in `ExchangeWithdrawActuator`) and throw `ContractExeException` on failure, mirroring the pattern already used in `TransferAssetActuator`, `ParticipateAssetIssueActuator`, and `Commons.adjustAssetBalanceV2`, so a failed asset mutation cannot be masked by a `SUCESS` result while the exchange pool balances have already been mutated.

### Proof of Concept
Not constructible from static code review alone: exploiting this requires making `assetBalanceEnoughV2` (validate-time) and `reduceAssetAmountV2` (execute-time) disagree on the same account/asset key within the same transaction, which I was unable to confirm from the code paths I could inspect. A background Devin session with full test-execution access would be needed to attempt to construct such a divergence (e.g., via `AllowSameTokenName` transitions, asset-map import timing in `importAsset`, or concurrent trace retry paths in `TransactionTrace`) and confirm exploitability end-to-end.

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L71-99)
```java
      if (Arrays.equals(tokenID, firstTokenID)) {
        anotherTokenID = secondTokenID;
        anotherTokenQuant = floorDiv(multiplyExact(
            secondTokenBalance, tokenQuant), firstTokenBalance);
        exchangeCapsule.setBalance(addExact(firstTokenBalance, tokenQuant),
            addExact(secondTokenBalance, anotherTokenQuant));
      } else {
        anotherTokenID = firstTokenID;
        anotherTokenQuant = floorDiv(multiplyExact(
            firstTokenBalance, tokenQuant), secondTokenBalance);
        exchangeCapsule.setBalance(addExact(firstTokenBalance, anotherTokenQuant),
            addExact(secondTokenBalance, tokenQuant));
      }

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
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L242-256)
```java
    } else {
      if (!accountCapsule.assetBalanceEnoughV2(tokenID, tokenQuant, dynamicStore)) {
        throw new ContractValidateException("token balance is not enough");
      }
    }

    if (Arrays.equals(anotherTokenID, TRX_SYMBOL_BYTES)) {
      if (accountCapsule.getBalance() < addExact(anotherTokenQuant, calcFee())) {
        throw new ContractValidateException("balance is not enough");
      }
    } else {
      if (!accountCapsule.assetBalanceEnoughV2(anotherTokenID, anotherTokenQuant, dynamicStore)) {
        throw new ContractValidateException("another token balance is not enough");
      }
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

**File:** actuator/src/main/java/org/tron/core/actuator/ParticipateAssetIssueActuator.java (L84-87)
```java
      toAccount.setBalance(addExact(toAccount.getBalance(), cost));
      if (!toAccount.reduceAssetAmountV2(key, exchangeAmount, dynamicStore, assetIssueStore)) {
        throw new ContractExeException("reduceAssetAmount failed !");
      }
```

**File:** chainbase/src/main/java/org/tron/common/utils/Commons.java (L131-149)
```java
  public static void adjustAssetBalanceV2(AccountCapsule account, String AssetID, long amount,
      AccountStore accountStore, AssetIssueStore assetIssueStore,
      DynamicPropertiesStore dynamicPropertiesStore)
      throws BalanceInsufficientException {
    if (amount < 0) {
      if (!account.reduceAssetAmountV2(AssetID.getBytes(), -amount, dynamicPropertiesStore,
          assetIssueStore)) {
        throw new BalanceInsufficientException(
            String.format("reduceAssetAmount failed! account: %s",
                    StringUtil.encode58Check(account.createDbKey())));
      }
    } else if (amount > 0 &&
        !account.addAssetAmountV2(AssetID.getBytes(), amount, dynamicPropertiesStore,
            assetIssueStore)) {
      throw new BalanceInsufficientException(
          String.format("addAssetAmount failed! account: %s",
                  StringUtil.encode58Check(account.createDbKey())));
    }
    accountStore.put(account.getAddress().toByteArray(), account);
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L93-97)
```java
      if (Arrays.equals(tokenID, TRX_SYMBOL_BYTES)) {
        accountCapsule.setBalance(addExact(newBalance, tokenQuant));
      } else {
        accountCapsule.addAssetAmountV2(tokenID, tokenQuant, dynamicStore, assetIssueStore);
      }
```
