### Title
Unchecked return value of `AccountCapsule.reduceAssetAmountV2` in `MarketSellAssetActuator._transferBalanceOrToken` - (File: `actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java`)

### Summary
`MarketSellAssetActuator.execute()` calls a private helper `transferBalanceOrToken(accountCapsule)` to debit the seller's sell-token balance before creating/matching a market order, but the helper discards the boolean success/failure result of `AccountCapsule.reduceAssetAmountV2(...)`, exactly mirroring the `Allo._fundPool` pattern of ignoring a transfer function's boolean return value.

### Finding Description
`transferBalanceOrToken` calls `accountCapsule.reduceAssetAmountV2(sellTokenID, sellTokenQuantity, dynamicStore, assetIssueStore)` without checking the returned boolean: [1](#0-0) 

`reduceAssetAmountV2` returns `false` (without throwing) whenever the asset key is not found in the account's asset map or the requested amount exceeds the current amount: [2](#0-1) 

This is the same shape of bug as the reported `Allo._fundPool` issue: a token-moving primitive that signals failure via a boolean return value is called without verifying that return value, and the caller proceeds as if the debit succeeded — creating the order (`createAndSaveOrder`), matching it against the order book (`matchOrder`), and paying out the corresponding buy-side tokens/TRX to counterparties — even in a scenario where the seller's balance was not actually reduced.

By contrast, other actuators in the codebase that call the same boolean-returning asset primitives explicitly check the result and abort on failure, e.g. `TransferAssetActuator` and `ParticipateAssetIssueActuator`: [3](#0-2) [4](#0-3) 

`MarketSellAssetActuator` does not apply the same defensive check to `reduceAssetAmountV2`.

### Impact Explanation
If `reduceAssetAmountV2` silently returns `false` in `transferBalanceOrToken`, the seller's asset balance is left untouched while the actuator still creates a fully-backed-looking sell order and lets it match against real buyer funds/tokens via `matchOrder`, effectively allowing tokens/TRX to be paid out to a counterparty without the seller's balance ever being debited — an unbacked-balance / asset-theft scenario, matching the impact class described in the Allo report.

### Likelihood Explanation
`validate()` currently checks `ownerAccount.assetBalanceEnoughV2(sellTokenID, sellTokenQuantity, dynamicStore)` before `execute()` runs, and under the current, unmodified code paths this pre-check makes the specific `reduceAssetAmountV2` failure condition (`currentAmount == null` or `amount > currentAmount`) unlikely to be hit in `execute()` given `validate()` and `execute()` are invoked back-to-back for the same transaction with no interleaving contract that mutates the same asset balance in between. This makes the current concrete exploitability low/uncertain: I could not identify a live TOCTOU window in the current codebase that lets an unprivileged caller force `reduceAssetAmountV2` to return `false` here today. The finding is therefore best framed as a latent/defense-in-depth gap — structurally identical to the reported bug class — rather than a proven, immediately triggerable loss of funds. Confidence in real-world exploitability is lower than the other checked call sites (e.g., `TransferAssetActuator`) precisely because those sites do perform the check while this one does not, and any future change to `validate()`/`execute()` ordering, to dynamic parameters (`AllowSameTokenName`) affecting which map is checked/updated, or to the order-matching flow could turn this into a directly exploitable path.

### Recommendation
Check the boolean return value of `reduceAssetAmountV2` in `transferBalanceOrToken` (and the equivalent `addAssetAmountV2` calls in `addTrxOrToken`) and throw a `ContractExeException` (as done in `TransferAssetActuator` and `ParticipateAssetIssueActuator`) if the operation fails, instead of allowing the order to proceed as if the balance transfer succeeded.

### Proof of Concept
Not independently reproducible with current tools/index access — the exploitability depends on finding a concrete state where `validate()`'s `assetBalanceEnoughV2` check and `execute()`'s `reduceAssetAmountV2` call observe different account/asset state (e.g., via a TOCTOU or dynamic-parameter change between the two calls). I was unable to confirm such a window exists in the current sequential validate→execute invocation model used by java-tron; a Devin session with full repository/test access would be needed to trace the exact actuator invocation order in `TransactionTrace`/`Manager` to confirm or rule out this window before treating the impact as concretely triggerable.

### Citations

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

**File:** actuator/src/main/java/org/tron/core/actuator/TransferAssetActuator.java (L76-79)
```java
      if (!ownerAccountCapsule
          .reduceAssetAmountV2(assetName.toByteArray(), amount, dynamicStore, assetIssueStore)) {
        throw new ContractExeException("reduceAssetAmount failed !");
      }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ParticipateAssetIssueActuator.java (L85-87)
```java
      if (!toAccount.reduceAssetAmountV2(key, exchangeAmount, dynamicStore, assetIssueStore)) {
        throw new ContractExeException("reduceAssetAmount failed !");
      }
```
