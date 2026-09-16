### Title
Unchecked overflow / unvalidated negative balances in non-hardened TRC10 Exchange price calculation - ([File: chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java])

### Summary
The reported bug class is an unchecked numeric cast/overflow in a price-calculation routine that lets a crafted input silently produce a wrong (negative-turned-positive, or otherwise corrupted) price that is then used without validation. The same bug class exists in java-tron's TRC10 Exchange (Bancor-formula) pricing path: `ExchangeCapsule.transaction()` only validates for post-transaction negative balances / overflow when the chain-parameter-gated "hardened" calculation path is active. In the default (non-hardened) path, the resulting balances from the unchecked double-based `ExchangeProcessor` calculation are written to the exchange pool state with no sanity check at all.

### Finding Description
`ExchangeCapsule.transaction()` selects between two calculation engines based on the `hardenedCalc` flag, which is derived from the chain parameter `allowHardenExchangeCalculation`: [1](#0-0) 

The default engine, `ExchangeProcessor`, performs the Bancor-style relay calculation entirely in `double` arithmetic and truncates the result with a plain `(long)` cast, with no overflow or bounds checking: [2](#0-1) 

Critically, back in `ExchangeCapsule.transaction()`, the post-calculation sanity check that rejects negative resulting balances is only executed when `hardenedCalc` is `true`: [3](#0-2) 

and the balance updates themselves use plain (non-overflow-checked) `long` addition/subtraction in the non-hardened branch: [4](#0-3) 

This is structurally identical to the reported bug class: a value is computed via an operation that can silently overflow/underflow or lose precision (there, `int256(timeCoefficient*timeBucket)`; here, `(long) issuedSupply`/`(long) exchangeBalance` from unchecked `double` math combined with unchecked `long` balance arithmetic), and the corrupted result is used to update on-chain state without any validating guard — the guard exists (`SafeExchangeProcessor` plus the `newFirstTokenBalance < 0 || newSecondTokenBalance < 0` check) but is only wired up conditionally on a proposal-controlled feature flag, so it does not protect the default execution path.

The transaction is reachable directly by any unprivileged account via `ExchangeTransactionContract`, handled in `ExchangeTransactionActuator.execute()`, which calls `exchangeCapsule.transaction(...)` and then commits the returned amounts to account balances/assets and to the exchange pool store without further verification of internal consistency: [5](#0-4) 

### Impact Explanation
If the double-based Bancor calculation in `ExchangeProcessor` produces an incorrect `buyTokenQuant` (due to floating-point precision loss, extreme balance ratios, or the truncating cast) large enough to exceed the actual pool reserve, the non-hardened code path will still commit `newSecondTokenBalance = secondTokenBalance - buyTokenQuant` even if that value goes negative, since the negative-balance guard is skipped entirely. A negative/corrupted pool balance persisted to the `ExchangeStore`/`ExchangeV2Store` corrupts the invariant that the exchange pool never pays out more than it holds, and combined with `accountCapsule.addAssetAmountV2`/`setBalance` in the actuator, this can result in a user receiving more TRX/TRC10 tokens than the pool actually has — i.e., an unbacked balance / theft of pool funds.

### Likelihood Explanation
The `ExchangeTransactionContract` (and `ExchangeCreateContract`/`ExchangeInjectContract`, which reuse the same processor) is a standard user-broadcastable transaction type requiring no special privilege, and `allowHardenExchangeCalculation` is a proposal/committee-toggled dynamic property. Unless and until that parameter is enabled on a given network, every exchange trade runs through the unguarded `ExchangeProcessor` path. Exploitability depends on finding balance/quant combinations that trigger sufficient floating-point drift or a cast/arithmetic edge case to flip the sign or magnitude of the computed amount; I was not able to fully verify within this pass whether the default value of `allowHardenExchangeCalculation` is 0 (disabled) or 1 (enabled) in `DynamicPropertiesStore.java`, since the exact default-value line was not captured in my search results — this should be confirmed before treating the likelihood as fully proven.

### Recommendation
Apply the same negative-balance/overflow validation performed in the hardened branch unconditionally, regardless of the `allowHardenExchangeCalculation` flag, or retire the non-hardened `ExchangeProcessor` path entirely in favor of always using `SafeExchangeProcessor` with `addExact`/`subtractExact` and the `newFirstTokenBalance < 0 || newSecondTokenBalance < 0` check.

### Proof of Concept
Not able to construct a concrete numeric PoC input within this pass (would require iterating extreme `firstTokenBalance`/`secondTokenBalance`/`sellTokenQuant` combinations against `ExchangeProcessor.exchangeToSupply`/`exchangeFromSupply` to find a case where floating-point drift or the truncating `(long)` cast produces a `buyTokenQuant` exceeding the real reserve). This gap should be closed with targeted differential testing between `ExchangeProcessor` and `SafeExchangeProcessor` across boundary values (very small `newBalance`, near-`Long.MAX_VALUE` balances, etc.) before treating this as conclusively exploitable.

### Citations

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-168)
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
```

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java (L17-39)
```java
  private long exchangeToSupply(long balance, long quant) {
    logger.debug("balance: " + balance);
    long newBalance = balance + quant;
    logger.debug("balance + quant: " + newBalance);

    double issuedSupply = -supply * (1.0
        - Maths.pow(1.0 + (double) quant / newBalance, 0.0005, this.useStrictMath));
    logger.debug("issuedSupply: " + issuedSupply);
    long out = (long) issuedSupply;
    supply += out;

    return out;
  }

  private long exchangeFromSupply(long balance, long supplyQuant) {
    supply -= supplyQuant;

    double exchangeBalance = balance
        * (Maths.pow(1.0 + (double) supplyQuant / supply, 2000.0, this.useStrictMath) - 1.0);
    logger.debug("exchangeBalance: " + exchangeBalance);

    return (long) exchangeBalance;
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L61-99)
```java
      byte[] firstTokenID = exchangeCapsule.getFirstTokenId();
      byte[] secondTokenID = exchangeCapsule.getSecondTokenId();

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
      ret.setStatus(fee, code.SUCESS);
```
