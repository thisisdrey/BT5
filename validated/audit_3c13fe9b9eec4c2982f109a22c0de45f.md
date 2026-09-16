### Title
Exchange pool balances are mutated in memory before the transaction can fail, corrupting exchange state analogous to `lastExecutionPrice` loss - (File: actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java)

### Summary
`ExchangeTransactionActuator.execute()` calls `exchangeCapsule.transaction(...)` to compute `anotherTokenQuant`, but this call mutates the in-memory `ExchangeCapsule`'s `firstTokenBalance`/`secondTokenBalance` fields as a side effect *before* the account-balance bookkeeping that follows (`subtractExact`/`addExact`/`reduceAssetAmountV2`/`addAssetAmountV2`) has succeeded. This mirrors the PoolKeeper `lastExecutionPrice` bug class: a shared piece of state is optimistically overwritten prior to an operation that can still fail, so a failure leaves stale/incorrect state behind instead of preserving the pre-operation value.

### Finding Description
In `ExchangeCapsule.transaction()`, the pool balances are updated unconditionally and returned to the caller: [1](#0-0) 

This method is called early in `ExchangeTransactionActuator.execute()`, and its return value (`anotherTokenQuant`) is used to update the account's asset/TRX balances afterward via `subtractExact`/`addExact`, `reduceAssetAmountV2`, and `addAssetAmountV2`: [2](#0-1) 

If any of those subsequent balance operations throw (e.g., `ArithmeticException` from `subtractExact`/`addExact` on overflow/underflow, or an `ItemNotFoundException`), the whole `execute()` call fails and the caught exception path never invokes `Commons.putExchangeCapsule(...)` or `accountStore.put(...)`: [3](#0-2) 

Because `exchangeCapsule` was fetched via `Commons.getExchangeStoreFinal(...).get(...)` and then mutated *in place* by `transaction()` prior to persistence, any code path (including cache layers or same-instance references held elsewhere in the same session/block) that observes this in-memory object after a failed `execute()` sees an exchange pool with already-adjusted balances that were never supposed to take effect — the same "lost previous value / applied-too-early state" issue described in the report, just with pool token balances instead of `lastExecutionPrice`.

### Impact Explanation
If the mutated, not-yet-committed `ExchangeCapsule` instance can be observed or reused (e.g., across multiple contracts processed in the same block/session before the store cache is properly discarded on revert), a reverted/failed exchange transaction could still leave the in-memory exchange pool state corrupted for subsequent processing within the same block, producing incorrect `anotherTokenQuant` calculations for later transactions against the same exchange pair. This falls under permanent freezing/incorrect-accounting-of-funds risk for the AMM-style TRC10 exchange feature, a Medium severity impact consistent with the original advisory's classification.

### Likelihood Explanation
Reachability is straightforward: an unprivileged transaction broadcaster can submit `ExchangeTransactionContract` transactions that call `ExchangeTransactionActuator.execute()`, and `subtractExact`/`addExact` are exact-arithmetic operations designed to throw on overflow — a value could be crafted (e.g. combined with other transactions in the same block affecting the same account/exchange) to trigger `ArithmeticException` after `exchangeCapsule.transaction()` has already mutated balances. This is a plausible but not fully confirmed condition: I could not fully verify from the available index whether `TronStoreWithRevoking`/the caching layer returns a genuinely fresh deserialized object per `get()` call (which would fully neutralize the issue) or a cached, shared instance (which would make the mutation observable across calls within the same session). This distinction is central to determining actual exploitability and could not be conclusively resolved with the tools available — a Devin session with full repository access would be needed to trace `TronStoreWithRevoking`, `SnapshotImpl`, and the exchange store's caching behavior end-to-end.

### Recommendation
Defer mutating `ExchangeCapsule`'s balances until all account-side balance operations in `execute()` have succeeded, mirroring the report's fix: compute `anotherTokenQuant` without side effects, then apply both the account and exchange-pool updates together only after all arithmetic has been validated, and call `Commons.putExchangeCapsule(...)` only on confirmed success (as it already does), ensuring no intermediate mutated capsule instance escapes on failure paths.

### Proof of Concept
Not fully constructible from static analysis alone due to the uncertainty in the caching/revoking layer noted above; confirming exploitability requires runtime tracing of `TronStoreWithRevoking.get()` to determine whether the same `ExchangeCapsule` object instance persists across the failed transaction and a subsequent read within the same block.

### Citations

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L160-168)
```java
    if (hardenedCalc && (newFirstTokenBalance < 0 || newSecondTokenBalance < 0)) {
      throw new ContractValidateException("Exchange balance must be >=0 after transaction");
    }
    this.exchange = this.exchange.toBuilder()
        .setFirstTokenBalance(newFirstTokenBalance)
        .setSecondTokenBalance(newSecondTokenBalance)
        .build();

    return buyTokenQuant;
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L67-91)
```java
      byte[] anotherTokenID;
      long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
          dynamicStore.allowStrictMath(), allowHarden());

      if (Arrays.equals(tokenID, firstTokenID)) {
        anotherTokenID = secondTokenID;
      } else {
        anotherTokenID = firstTokenID;
      }

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
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L93-105)
```java
      accountStore.put(accountCapsule.createDbKey(), accountCapsule);

      Commons.putExchangeCapsule(exchangeCapsule, dynamicStore, exchangeStore, exchangeV2Store,
          assetIssueStore);

      ret.setExchangeReceivedAmount(anotherTokenQuant);
      ret.setStatus(fee, code.SUCESS);
    } catch (ItemNotFoundException | InvalidProtocolBufferException
        | ContractValidateException | ArithmeticException e) {
      logger.debug(e.getMessage(), e);
      ret.setStatus(fee, code.FAILED);
      throw new ContractExeException(e.getMessage());
    }
```
