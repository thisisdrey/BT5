### Title
Floating-point precision loss in the legacy Bancor-style Exchange formula causes on-chain AMM reserve ledger to diverge from the value actually escrowed, enabling extraction of more tokens than deposited - ([File: chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java])

### Summary
The TRX/TRC10 built-in Exchange (Bancor-relay AMM) maintains an internal ledger of reserve balances (`firstTokenBalance`/`secondTokenBalance`) in `ExchangeCapsule`, which is supposed to always represent the actual tokens held on behalf of liquidity providers/traders. Unless a specific committee proposal (`allowHardenExchangeCalculation`) has been activated, every trade/inject/withdraw uses the legacy `ExchangeProcessor`, which computes trade output with `double`-precision floating point (`Math.pow`) instead of exact arithmetic. This is functionally the same bug class as the reported Masset issue: an internal accounting value (the vault/reserve balance) is updated from a *computed/expected* amount rather than a value that is provably backed, and the divergence between the theoretical ledger and the true backing value can be exploited by any caller of `ExchangeTransactionActuator`, `ExchangeInjectActuator`, or `ExchangeWithdrawActuator`.

### Finding Description
`ExchangeCapsule.transaction()` selects between two arithmetic engines based on a dynamic property flag: [1](#0-0) 

When the hardened flag is *not* active (the default state until a committee proposal activates it), the trade quantity is computed by `ExchangeProcessor`, which relies entirely on IEEE-754 `double` math and `Math.pow`: [2](#0-1) 

The resulting `buyTokenQuant`/`anotherTokenQuant` is then used, without any independent verification against actual token movement, to both credit the trader's account and to update the exchange's internal reserve ledger: [3](#0-2) [4](#0-3) [5](#0-4) 

This mirrors the report's root cause exactly: the "vault"/pool accounting (`ExchangeCapsule.firstTokenBalance`/`secondTokenBalance`) is derived from a formula's *expected* output rather than from a value that is guaranteed to be consistent with what was actually deposited/withdrawn from real user balances. The project's own test suite for the new `SafeExchangeProcessor` (BigDecimal-based) explicitly demonstrates that the legacy `double`-based processor's output differs from the exact result: [6](#0-5) 

i.e. `Assert.assertNotEquals(anotherTokenQuant, result)` — the legacy processor is proven, by the codebase's own tests, to produce a numerically different (imprecise) result than the exact/hardened calculation across many realistic reserve/quantity combinations.

### Impact Explanation
Because trade output and reserve-ledger updates are driven by the same imprecise floating-point value, repeated trades (which any unprivileged account can broadcast via `ExchangeTransactionContract`) can accumulate rounding bias in the AMM reserves. Over many transactions this can move the internal ledger away from the true, economically-backed reserve ratio, letting an attacker extract more of a token than the pool should be able to give while another user's later trade or withdrawal computes using the now-skewed reserve figures — i.e., a form of unbacked balance/value leakage from the AMM, analogous to the mAsset vault-balance/lending-pool divergence in the source report. This affects funds held collectively by all liquidity participants in a given Exchange pair, not just the caller, which is a Medium/High-severity fund-safety issue.

### Likelihood Explanation
Any account can create an exchange pair and trade against it using ordinary, unprivileged transactions (`ExchangeCreateContract`, `ExchangeTransactionContract`, `ExchangeInjectContract`, `ExchangeWithdrawContract`) — no special privilege is required. The vulnerable legacy path is the *default* code path whenever the `allowHardenExchangeCalculation` committee proposal has not been activated on a given network, which the codebase's own gating logic in `AbstractExchangeActuator.allowHarden()` confirms is a runtime-toggle, not a compile-time removal of the legacy processor: [7](#0-6) 
So exploitability is contingent on that dynamic parameter's on-chain value, which I could not directly confirm (default value/activation status) within the available tool budget — this is the main residual uncertainty.

### Recommendation
Retire the double-precision `ExchangeProcessor` entirely (rather than gating it behind an optional proposal) and make `SafeExchangeProcessor`'s exact `BigDecimal` arithmetic the only code path for `ExchangeCapsule.transaction()`, `ExchangeInjectActuator`, and `ExchangeWithdrawActuator`, so the on-chain reserve ledger can never diverge from an exact, reproducible calculation regardless of proposal activation state.

### Proof of Concept
1. Deploy on a network where `allowHardenExchangeCalculation` proposal has not been activated (default legacy behavior).
2. Create an Exchange pair (`ExchangeCreateContract`) with two TRC10 tokens/TRX.
3. Repeatedly submit `ExchangeTransactionContract` trades with quantities chosen (per the differing outputs shown in `ExchangeProcessorTest.testStrictMath`, e.g. reserve/quant pairs like `{4732214L, 2202692725330L, 29218L}`) so that the legacy `double`-based `ExchangeProcessor.exchange()` consistently returns a value larger than the exact/hardened value returned by `SafeExchangeProcessor.INSTANCE.exchange()` for the same input.
4. Because the reserve ledger (`firstTokenBalance`/`secondTokenBalance`) is updated using the (over-)computed value each time, iterating this trade accumulates a persistent divergence between the ledger and the true backing value, eventually allowing the attacker (or draining liquidity providers) to withdraw more value than was ever deposited.

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

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java (L17-45)
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

  @Override
  public long exchange(long sellTokenBalance, long buyTokenBalance, long sellTokenQuant) {
    long relay = exchangeToSupply(sellTokenBalance, sellTokenQuant);
    return exchangeFromSupply(buyTokenBalance, relay);
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L64-91)
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L77-104)
```java
      if (Arrays.equals(tokenID, firstTokenID)) {
        anotherTokenID = secondTokenID;
        anotherTokenQuant = bigSecondTokenBalance.multiply(bigTokenQuant)
            .divide(bigFirstTokenBalance).longValueExact();
        exchangeCapsule.setBalance(subtractExact(firstTokenBalance, tokenQuant),
            subtractExact(secondTokenBalance, anotherTokenQuant));
      } else {
        anotherTokenID = firstTokenID;
        anotherTokenQuant = bigFirstTokenBalance.multiply(bigTokenQuant)
            .divide(bigSecondTokenBalance).longValueExact();
        exchangeCapsule.setBalance(subtractExact(firstTokenBalance, anotherTokenQuant),
            subtractExact(secondTokenBalance, tokenQuant));
      }

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

**File:** framework/src/test/java/org/tron/core/capsule/utils/ExchangeProcessorTest.java (L272-280)
```java
    for (long[] data : testData) {
      ExchangeProcessor processor = new ExchangeProcessor(supply, false);
      long anotherTokenQuant = processor.exchange(data[0], data[1], data[2]);
      processor = new ExchangeProcessor(supply, true);
      long result = processor.exchange(data[0], data[1], data[2]);
      long safeResult = SafeExchangeProcessor.INSTANCE.exchange(data[0], data[1], data[2]);
      Assert.assertNotEquals(anotherTokenQuant, result);
      Assert.assertEquals(safeResult, result);
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/AbstractExchangeActuator.java (L13-15)
```java
  protected boolean allowHarden() {
    return chainBaseManager.getDynamicPropertiesStore().allowHardenExchangeCalculation();
  }
```
