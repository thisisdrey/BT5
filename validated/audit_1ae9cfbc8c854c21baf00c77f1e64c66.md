### Title
Unchecked `reduceAssetAmountV2` return value in `MarketSellAssetActuator.transferBalanceOrToken` allows credited buy-token/TRX to be issued without a corresponding debit - ([File: actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java])

### Summary
This finding maps the report's core defect class — a state-crediting operation ("mint"/deposit) proceeding even though the paired debit/authorization step silently fails because its boolean return value is unchecked — onto `MarketSellAssetActuator`. `transferBalanceOrToken` calls `accountCapsule.reduceAssetAmountV2(sellTokenID, sellTokenQuantity, dynamicStore, assetIssueStore)` and completely discards the boolean success/failure result, exactly like the reported unchecked `transferFrom` return value that let `deposit` mint vault tokens regardless of whether funds actually moved.

### Finding Description
`MarketSellAssetActuator.execute()` runs `transferBalanceOrToken(accountCapsule)` [1](#0-0)  which for non-TRX sell tokens calls `reduceAssetAmountV2` and ignores its boolean result: [2](#0-1) . `AccountCapsule.reduceAssetAmountV2` returns `false` (a no-op, balances unchanged) rather than throwing when the debit cannot be satisfied [3](#0-2) . Because the return value is discarded, execution unconditionally proceeds to `createAndSaveOrder` and `matchOrder`, which can credit the counterparty (maker) with the buy token via `addTrxOrToken`/`addAssetAmountV2` (also unchecked) [4](#0-3) , and persists the order/account state regardless of whether the seller's asset was actually debited [5](#0-4) .

This mirrors the Trading.sol pattern precisely: a boolean-returning balance-affecting call (`transferFrom` there, `reduceAssetAmountV2` here) whose failure is never checked, followed by an unconditional "mint"/credit step (`StableVault.deposit()` there, order creation + counterparty asset credit here) that produces value without a backing debit.

### Impact Explanation
If `reduceAssetAmountV2` fails silently (e.g., due to any state divergence between `validate()`'s `assetBalanceEnoughV2` check and the state actually mutated in `execute()` — such as `AllowSameTokenName` map-selection differences, or bugs/edge-cases in `importAsset`/`disableJavaLangMath`), the seller's asset is not actually reduced while the sell order is still created and can be matched, letting a counterparty receive real value (TRX or another TRC10 asset) for a sell order that was never actually funded. This is an unbacked-balance / unauthorized-value-creation scenario matching the report's severity class (funds minted/credited without a genuine backing transfer).

### Likelihood Explanation
This code path is reachable directly by any unprivileged account via `MarketSellAssetContract` (an ordinary broadcastable transaction type, order placer). Exploitation requires that `reduceAssetAmountV2` return `false` while `validate()`'s prior `assetBalanceEnoughV2` check passed — under strictly sequential single-transaction processing (validate immediately followed by execute on the same store state) this specific divergence is not obviously triggerable today, so likelihood is lower than a fully proven exploit; however, the missing check itself is a genuine defect (fragile invariant, not defense-in-depth) that removes the safety net if any future change (V1/V2 asset map desync, concurrent proposal activation, refactor) breaks the validate/execute symmetry.

### Recommendation
Check the boolean return value of `reduceAssetAmountV2` (and `addAssetAmountV2`) in `MarketSellAssetActuator.transferBalanceOrToken`/`addTrxOrToken` and throw `ContractExeException`/abort the trade if the debit fails, instead of silently continuing to create/match orders. Apply the same defensive check pattern used elsewhere (e.g., `TransferAssetActuator` throws `ContractExeException` when `reduceAssetAmountV2` fails) [6](#0-5)  uniformly across all Exchange/Market actuators (`ExchangeInjectActuator`, `ExchangeWithdrawActuator`, `ExchangeTransactionActuator`, `MarketSellAssetActuator`) that currently call `reduceAssetAmountV2`/`addAssetAmountV2` without checking the result [7](#0-6) .

### Proof of Concept
Not independently reproducible from static analysis alone: it requires demonstrating a concrete state under which `AccountCapsule.assetBalanceEnoughV2` (used in `validate()`) and `reduceAssetAmountV2` (used in `execute()`) disagree for the same account/token within one transaction's processing window. I could not construct or confirm such a divergence with the tools available, so this should be treated as a defect-in-depth (unchecked critical return value) rather than a fully proven exploit — flagged explicitly as unverified likelihood per the analysis above.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L133-134)
```java
      // 1. transfer of balance
      transferBalanceOrToken(accountCapsule);
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L143-151)
```java
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

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L537-561)
```java
  // for taker
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
```

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L88-99)
```java
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
