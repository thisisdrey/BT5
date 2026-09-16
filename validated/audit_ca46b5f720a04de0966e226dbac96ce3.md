## Analog Found: Unchecked return value of internal asset‑transfer operations in `ExchangeTransactionActuator`

### Title
Unhandled failure of `reduceAssetAmountV2`/`addAssetAmountV2` allows inconsistent Exchange state and unbacked token balances - (File: `actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java`)

### Summary
The Cooler report flags that `transferFrom`/`transfer` calls are used without checking their success return value, letting a `Request` be created even though the collateral transfer silently failed. The equivalent pattern exists in java-tron's `ExchangeTransactionActuator.execute()`, where the "sell"/"buy" leg of a TRC10 asset exchange is performed via `AccountCapsule.reduceAssetAmountV2()` / `addAssetAmountV2()` — both of which return a `boolean` success indicator — but the return value is discarded.

### Finding Description
In `ExchangeTransactionActuator.execute()`, after computing the exchanged amount via `exchangeCapsule.transaction(...)`, the actuator mutates account balances directly: [1](#0-0) 

Both `accountCapsule.reduceAssetAmountV2(tokenID, tokenQuant, ...)` and `accountCapsule.addAssetAmountV2(anotherTokenID, anotherTokenQuant, ...)` return `boolean` to indicate whether the asset balance operation succeeded, exactly like `AccountCapsule` methods elsewhere in the codebase. This is proven by the sibling actuator `TransferAssetActuator`, which explicitly checks this same return value and throws when it is `false`: [2](#0-1) 

`ExchangeTransactionActuator` does not perform this check. If `reduceAssetAmountV2` returns `false` (e.g., because the token map entry is missing/corrupted or an edge-case in asset accounting causes it to fail without throwing), execution continues: the exchange pool's `firstTokenBalance`/`secondTokenBalance` computed by `exchangeCapsule.transaction(...)` are still committed via `Commons.putExchangeCapsule(...)`, and the "buy" side asset is still credited to the same account via `addAssetAmountV2`, while the "sell" side deduction that should have backed it silently did nothing. The account and exchange capsules are persisted regardless: [3](#0-2) 

This mirrors the Cooler pattern precisely: an operation whose success/failure is communicated via a return value (not an exception) is treated as always successful, and downstream state (loan record in Cooler; exchange pool + credited balance in java-tron) is committed unconditionally.

### Impact Explanation
An unprivileged order placer submitting an `ExchangeTransactionContract` could, under a code path where `reduceAssetAmountV2` returns `false` instead of throwing, receive the "bought" token (`anotherTokenQuant`) credited to their account while the corresponding "sold" token debit never occurred. This produces an unbacked token balance increase relative to the on-chain exchange pool's tracked reserves, since the pool balances (`ExchangeCapsule`) are updated as though the trade was fully paid for by the caller's actual token holdings. This is a fund-accounting integrity issue reachable from a single signed transaction (`ExchangeTransactionContract`) by any account holder.

### Likelihood Explanation
Likelihood depends on whether `reduceAssetAmountV2`/`addAssetAmountV2` can realistically return `false` under conditions reachable via `ExchangeTransactionActuator`'s `validate()` checks (which validate `assetBalanceEnoughV2` beforehand, reducing — but not necessarily eliminating — the chance of divergence between validate-time and execute-time state, e.g., due to reentrancy-like sequencing within the same transaction or state changes from unrelated operations in the same block). The exact internal failure conditions of `AccountCapsule.reduceAssetAmountV2`/`addAssetAmountV2` were not fully inspected in this pass (the method bodies in `chainbase/src/main/java/org/tron/core/capsule/AccountCapsule.java` were located but not read in detail), so the precise triggering scenario is not fully confirmed — this should be verified against the actual implementation before treating the severity as certain.

### Recommendation
Check the boolean return values of `reduceAssetAmountV2` and `addAssetAmountV2` in `ExchangeTransactionActuator.execute()`, and throw a `ContractExeException` (aborting the actuator, as `TransferAssetActuator` already does) if either operation fails, before persisting the account or exchange capsules.

### Proof of Concept
Not independently reproduced; the code-path support above (comparing `TransferAssetActuator`'s explicit check against `ExchangeTransactionActuator`'s missing check) is offered as the structural evidence. Confirming exploitability requires locating a concrete state under which `AccountCapsule.reduceAssetAmountV2`/`addAssetAmountV2` return `false` rather than throw, which was not fully verified in this analysis due to tool-call limits.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L80-99)
```java
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

**File:** actuator/src/main/java/org/tron/core/actuator/TransferAssetActuator.java (L75-79)
```java
      AccountCapsule ownerAccountCapsule = accountStore.get(ownerAddress);
      if (!ownerAccountCapsule
          .reduceAssetAmountV2(assetName.toByteArray(), amount, dynamicStore, assetIssueStore)) {
        throw new ContractExeException("reduceAssetAmount failed !");
      }
```
