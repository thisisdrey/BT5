### Title
Silent failure of `reduceAssetAmountV2`/`addAssetAmountV2` return values in `ExchangeInjectActuator`/`ExchangeWithdrawActuator` - (File: `actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java`)

### Summary
`AccountCapsule.reduceAssetAmountV2()` and `AccountCapsule.addAssetAmountV2()` are boolean-returning "transfer"-style methods that indicate whether an asset balance mutation actually succeeded. In `ExchangeInjectActuator.execute()` and `ExchangeWithdrawActuator.execute()`, these return values are discarded, unlike the equivalent calls in `TransferAssetActuator` and `ParticipateAssetIssueActuator`, which explicitly check the boolean and throw `ContractExeException` on failure.

### Finding Description
`AccountCapsule.reduceAssetAmountV2` returns `false` (and leaves the account state unchanged) whenever the amount is non-positive or the current asset balance is insufficient/absent, instead of throwing: [1](#0-0) 

Other actuators that mutate asset balances treat this boolean as authoritative and abort the whole state transition if it is `false`: [2](#0-1) [3](#0-2) 

In contrast, `ExchangeInjectActuator.execute()` calls `reduceAssetAmountV2` twice and ignores the result for both the injected token and the "another token" leg of the trade: [4](#0-3) 

`ExchangeWithdrawActuator.execute()` has the mirrored issue with `addAssetAmountV2`: [5](#0-4) 

In both actuators, the `ExchangeCapsule` pool balances (`exchangeCapsule.setBalance(...)`) and the on-chain `AccountStore`/`ExchangeStore` writes proceed unconditionally regardless of whether the underlying account-side asset debit/credit actually happened. Because the return value is never inspected, a failed `reduceAssetAmountV2`/`addAssetAmountV2` produces no exception, no rollback, and no log — an entirely silent failure, exactly the bug class described in the reference report (unchecked `transfer()` return value in `SdtBlackHole.withdraw()`).

### Impact Explanation
If `reduceAssetAmountV2` returns `false` in `ExchangeInjectActuator.execute()` while the corresponding `exchangeCapsule.setBalance()` update still commits, the AMM-style exchange pool's recorded balance for that token increases without the caller's account balance actually being debited. This creates an unbacked increase in the exchange pool's token accounting relative to the caller's real holdings — a form of accounting desync that other participants trading against the pool (via `ExchangeTransactionActuator`) rely on for correct pricing/backing. Symmetrically, a silently-failed `addAssetAmountV2` in `ExchangeWithdrawActuator` would decrease the exchange pool's tracked balance without crediting the withdrawing account, causing funds to become unaccounted-for/effectively frozen. Both are unauthorized-accounting/fund-integrity issues reachable by any account holder that owns an exchange pair, without any special privilege — satisfying the "unbacked balance" / "freezing of funds" impact bar.

### Likelihood Explanation
`validate()` in `ExchangeInjectActuator` does check `assetBalanceEnoughV2` before `execute()` runs, which is expected to make `reduceAssetAmountV2` succeed under the common case, so on a well-behaved single node, the failure path is not trivially triggered by an ordinary call. However, this is a defense-in-depth gap: it is the only exchange actuator among its siblings (`TransferAssetActuator`, `ParticipateAssetIssueActuator`) that fails to validate the mutation's own return value at the point of use, so any future divergence between the pre-check (`assetBalanceEnoughV2`) and the actual mutation (`reduceAssetAmountV2`/`addAssetAmountV2`) — e.g. due to an edge case in asset-map migration (`importAsset`), a bug introduced during maintenance, or reliance on stale `AccountCapsule` state fetched earlier in `execute()` — would go completely undetected and commit inconsistent state to the chain. I could not fully verify a concrete scenario in which `assetBalanceEnoughV2` and `reduceAssetAmountV2` diverge within a single `execute()` call given the current code; further live/dynamic verification (e.g. fuzzing `importAsset` edge cases and `AllowSameTokenName` transitions) would be needed to establish a fully deterministic proof-of-concept.

### Recommendation
Check the boolean return values of `reduceAssetAmountV2`/`addAssetAmountV2` in `ExchangeInjectActuator.execute()` and `ExchangeWithdrawActuator.execute()`, mirroring the pattern already used in `TransferAssetActuator`/`ParticipateAssetIssueActuator`:
```java
if (!accountCapsule.reduceAssetAmountV2(tokenID, tokenQuant, dynamicStore, assetIssueStore)) {
  throw new ContractExeException("reduceAssetAmount failed !");
}
```
and equivalently for `addAssetAmountV2` in the withdraw actuator, ensuring the exchange pool balance update and the account-side mutation cannot diverge.

### Proof of Concept
Static code comparison (no dynamic PoC executed):
1. `TransferAssetActuator.execute()` and `ParticipateAssetIssueActuator.execute()` both wrap `reduceAssetAmountV2` in `if (!...) throw new ContractExeException(...)`.
2. `ExchangeInjectActuator.execute()` (lines 91, 98) and `ExchangeWithdrawActuator.execute()` (lines 96, 103) call the same boolean-returning methods but discard the result, while still unconditionally committing `exchangeCapsule.setBalance(...)` and `accountStore.put(...)`.
3. Any account that is the creator of an `Exchange` pair can broadcast an `ExchangeInjectContract`/`ExchangeWithdrawContract` transaction that reaches this code path, so the reachable trigger requires only a standard signed transaction — no special privilege.

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L85-99)
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
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L91-104)
```java
      long newBalance = subtractExact(accountCapsule.getBalance(), calcFee());

      if (Arrays.equals(tokenID, TRX_SYMBOL_BYTES)) {
        accountCapsule.setBalance(addExact(newBalance, tokenQuant));
      } else {
        accountCapsule.addAssetAmountV2(tokenID, tokenQuant, dynamicStore, assetIssueStore);
      }

      if (Arrays.equals(anotherTokenID, TRX_SYMBOL_BYTES)) {
        accountCapsule.setBalance(addExact(newBalance, anotherTokenQuant));
      } else {
        accountCapsule
            .addAssetAmountV2(anotherTokenID, anotherTokenQuant, dynamicStore, assetIssueStore);
      }
```
