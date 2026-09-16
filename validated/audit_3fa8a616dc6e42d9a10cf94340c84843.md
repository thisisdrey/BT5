Based on the investigation, I found a structurally similar issue in `ExchangeTransactionActuator`, java-tron's analog of a bonding-curve buy/sell with slippage protection (`expected` parameter).

### Title
Non-idempotent, mutating slippage check in `ExchangeTransactionActuator` allows the executed trade to diverge from the validated `expected` amount - (File: `actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java`)

### Summary
The reported bug is a slippage-protection bypass caused by performing the minimum-output check (`minOrderSize`) against a value that is not the one ultimately used to fulfill the trade (the value is recalculated/reassigned afterward without re-checking it). The java-tron `ExchangeTransactionActuator` contains the same class of defect: the `expected` (minimum-received) slippage check in `doValidate()` and the actual token-credit calculation in `execute()` are two **separate invocations** of `ExchangeCapsule.transaction(...)`, and that method is not a pure/read-only calculation — it mutates the `ExchangeCapsule`'s pool balances as a side effect.

### Finding Description
`doValidate()` computes the amount of tokens the caller will receive by calling the mutating pool-pricing function and checks it against the caller-supplied `expected` value: [1](#0-0) 

`execute()` independently re-invokes the same mutating function to determine the actual amount credited to the account, with no re-check of `tokenExpected` at that point: [2](#0-1) 

`ExchangeCapsule.transaction(...)`, the shared pricing routine used by both call sites, is not a pure quote function — it directly mutates the capsule's `firstTokenBalance`/`secondTokenBalance` fields as a side effect of computing the trade output: [3](#0-2) 

This mirrors the root cause pattern in the external report: the value that is checked against the user's minimum-received guarantee (`tokenExpected`) is not guaranteed to be the same value that is finally applied to move funds, because the two computations are structurally decoupled (one in `validate()`, one in `execute()`) rather than the `expected` check being performed once, at the end, against the final applied amount. Any discrepancy between the two calls — whether from caching/reuse of the same `ExchangeCapsule` object across the two fetches (so the second `transaction()` call operates on an already-mutated pool state from the first) or from any future refactor that changes one call site but not the other — silently breaks the slippage guarantee, exactly as described in the report ("moving the check to the end... to ensure it is applied after all other conditions").

### Impact Explanation
If the two `transaction()` calls diverge (e.g., because the fetched `ExchangeCapsule` instance carries over the mutation performed during `validate()`), the token amount actually credited to the user's account in `execute()` can be lower than `tokenExpected`, which is precisely the value the `expected` parameter is meant to guarantee. This defeats the purpose of the slippage-protection field in `ExchangeTransactionContract` and can result in users receiving fewer tokens than their minimum acceptable amount for a bonding-curve-style AMM exchange, i.e., an unbacked/incorrect balance outcome for the trader.

### Likelihood Explanation
The double-invocation pattern is present in the shipped code unconditionally for every `ExchangeTransactionContract` processed on-chain — every buy/sell against the built-in TRX↔token AMM exchange goes through this exact `doValidate()`/`execute()` split. Any unprivileged account can submit an `ExchangeTransactionContract` to trigger this path, so the reachable surface matches the required "single signed transaction" threat model.

### Recommendation
Refactor `ExchangeTransactionActuator` so the `expected`/slippage check is derived from, and performed immediately before applying, the single canonical output of the exchange calculation used in `execute()` — i.e., compute `anotherTokenQuant` exactly once (ideally within `execute()`, right before the balances are persisted) and re-validate `anotherTokenQuant >= tokenExpected` at that point, rather than relying on a separate `validate()`-time computation of the same mutating function. Additionally, `ExchangeCapsule.transaction(...)` should not mutate internal state as a side effect of what is used as a "quote"/check calculation in `validate()`; a pure, non-mutating variant should be used for the `expected` check.

### Proof of Concept
Conceptual PoC: submit an `ExchangeTransactionContract` with `expected` set to the exact minimum the pool currently quotes via `exchangeCapsule.transaction(...)` in `doValidate()`. If the `ExchangeCapsule` instance retrieved in `execute()` reflects the balance mutation already applied by the `validate()`-time call (same object, or any staleness between the two fetches), the second `transaction()` invocation in `execute()` computes a smaller `anotherTokenQuant` against the now-shifted pool price, which is credited to the account without any further `tokenExpected` check — reproducible by unit-testing `ExchangeTransactionActuator.validate()` immediately followed by `.execute()` on the same `ExchangeCapsule` instance and comparing the two `transaction()` outputs. [1](#0-0) [2](#0-1) [3](#0-2) 

**Caveat:** I was unable to confirm within the available searches whether `ExchangeStore`/`ExchangeV2Store.get()` (backed by `TronStoreWithRevoking`) returns a freshly-deserialized `ExchangeCapsule` on each call or a cached/shared instance across the two fetches in `validate()` and `execute()`. This detail determines whether the divergence is directly exploitable today or is currently masked by re-deserialization; regardless, the non-idempotent, side-effecting design of the shared quote/execute function is a confirmed structural analog to the reported bug class and should be corrected.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L64-98)
```java
      byte[] tokenID = exchangeTransactionContract.getTokenId().toByteArray();
      long tokenQuant = exchangeTransactionContract.getQuant();

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

      accountStore.put(accountCapsule.createDbKey(), accountCapsule);

      Commons.putExchangeCapsule(exchangeCapsule, dynamicStore, exchangeStore, exchangeV2Store,
          assetIssueStore);

      ret.setExchangeReceivedAmount(anotherTokenQuant);
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L217-221)
```java
    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
    }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-169)
```java
  public long transaction(byte[] sellTokenID, long sellTokenQuant, boolean useStrictMath,
      boolean hardenedCalc) throws ContractValidateException {
    long supply = 1_000_000_000_000_000_000L;
    Processor processor = hardenedCalc
        ? SafeExchangeProcessor.INSTANCE : new ExchangeProcessor(supply, useStrictMath);

    long buyTokenQuant = 0;
    long firstTokenBalance = this.exchange.getFirstTokenBalance();
    long secondTokenBalance = this.exchange.getSecondTokenBalance();
    long newFirstTokenBalance;
    long newSecondTokenBalance;

    if (this.exchange.getFirstTokenId().equals(ByteString.copyFrom(sellTokenID))) {
      buyTokenQuant = processor.exchange(firstTokenBalance,
          secondTokenBalance,
          sellTokenQuant);
      newFirstTokenBalance = hardenedCalc
          ? StrictMathWrapper.addExact(firstTokenBalance, sellTokenQuant)
          : firstTokenBalance + sellTokenQuant;
      newSecondTokenBalance = hardenedCalc
          ? StrictMathWrapper.subtractExact(secondTokenBalance, buyTokenQuant)
          : secondTokenBalance - buyTokenQuant;

    } else {
      buyTokenQuant = processor.exchange(secondTokenBalance,
          firstTokenBalance,
          sellTokenQuant);
      newFirstTokenBalance = hardenedCalc
          ? StrictMathWrapper.subtractExact(firstTokenBalance, buyTokenQuant)
          : firstTokenBalance - buyTokenQuant;
      newSecondTokenBalance = hardenedCalc
          ? StrictMathWrapper.addExact(secondTokenBalance, sellTokenQuant)
          : secondTokenBalance + sellTokenQuant;

    }

    if (hardenedCalc && (newFirstTokenBalance < 0 || newSecondTokenBalance < 0)) {
      throw new ContractValidateException("Exchange balance must be >=0 after transaction");
    }
    this.exchange = this.exchange.toBuilder()
        .setFirstTokenBalance(newFirstTokenBalance)
        .setSecondTokenBalance(newSecondTokenBalance)
        .build();

    return buyTokenQuant;
  }
```
