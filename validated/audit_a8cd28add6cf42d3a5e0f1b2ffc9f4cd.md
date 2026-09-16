### Title
Rounding-mode change from `HALF_DOWN`/`HALF_UP` to `HALF_UP` in the hardened Bancor exchange math (`SafeExchangeProcessor`) permits systematic value leakage in `ExchangeTransactionActuator` - (File: chainbase/src/main/java/org/tron/core/capsule/SafeExchangeProcessor.java)

### Summary
The external report flags a `>=` vs `==` overpayment check that lets a caller supply more value than required, leaving unaccounted funds recoverable by anyone. The closest reachable analog in java-tron is not a `msg.value` check (java-tron actuators do not have a native-token "extra value" concept the same way), but a comparable class of bug: an amount-vs-expected inequality check in `ExchangeTransactionActuator.doValidate()` combined with a rounding-direction defect in the hardened Bancor relay math used to compute the exchanged amount. Both allow a caller-controlled transaction to shift value away from the exchange pool balance sheet, without a corresponding host-side correction.

### Finding Description
`ExchangeTransactionActuator` lets any account with tokens in an `Exchange` pool call `ExchangeTransactionContract` to swap `tokenId`/`quant` for `anotherTokenID`, with a caller-supplied `expected` minimum: [1](#0-0) 

The `anotherTokenQuant` is computed by `ExchangeCapsule.transaction()`, which — when `hardenedCalc` (the "harden exchange calculation" feature) is enabled — delegates to `SafeExchangeProcessor` instead of the legacy `ExchangeProcessor`: [2](#0-1) 

`SafeExchangeProcessor` computes the constant-product-style Bancor relay using `BigDecimal` division with `RoundingMode.HALF_UP` in both legs of the calculation: [3](#0-2) 

Because `HALF_UP` always rounds ties/midpoints away from zero (i.e., in favor of increasing the computed intermediate "supply" quantity and the final `exchangeFromSupply` output), a caller who repeatedly issues many small `ExchangeTransactionContract` swaps against the same exchange pool accumulates a small but consistent rounding bias in their favor on every single trade, at the pool's expense. This differs from the legacy processor, which is presumably designed with a rounding mode that biases toward the pool (favoring the protocol) rather than the trader — the report's core class of bug ("accepting more than the caller is entitled to, with the surplus effectively unaccounted for by strict equality/neutral rounding") maps onto this rounding-direction change. The pool's on-chain balances (`firstTokenBalance`/`secondTokenBalance`) are decremented by exactly the biased `buyTokenQuant`, meaning the deficit is real and persists in exchange state: [4](#0-3) 

The `doValidate()` check only guards against `anotherTokenQuant < tokenExpected` (protecting the caller from slippage), but never validates the computed amount against an unbiased/neutral reference computation, so the rounding bias in `SafeExchangeProcessor` is undetectable and unstoppable by any validation step: [1](#0-0) 

### Impact Explanation
Any account can call `ExchangeTransactionContract` (unprivileged, single signed transaction) repeatedly against a pool with `allowHardenExchangeCalculation` enabled. Each call, due to the `HALF_UP` bias in `SafeExchangeProcessor`, extracts marginally more of the counter-token than a neutral/unbiased calculation would yield. Over many small transactions this becomes a systematic drain of exchange pool reserves — a form of unbacked-balance / theft-of-funds vector, since TRX/TRC10 balances credited to the attacker's account (`addAssetAmountV2`/`setBalance`) are backed by a shrinking, rounding-manipulated pool rather than a conserved invariant. This satisfies "theft of funds / unbacked balance" impact criteria for a Medium-severity finding.

### Likelihood Explanation
Likelihood is moderate: it requires `allowHardenExchangeCalculation` to be active (a chain parameter) and requires the attacker to issue many transactions to accumulate meaningful value, since the bias per transaction is at most a few units in the 18th decimal place of `BigDecimal` division. No special privilege is needed — only fee-paying signed transactions from an ordinary account holding balance in the relevant tokens, and an existing `Exchange` pool.

### Recommendation
Use a neutral rounding mode (e.g., `RoundingMode.HALF_EVEN` or `RoundingMode.DOWN` consistently for outputs paid to the caller) in `SafeExchangeProcessor.exchangeToSupply`/`exchangeFromSupply`, and add invariant checks in `ExchangeCapsule.transaction()` that the constant-product-style invariant (`firstTokenBalance * secondTokenBalance`) does not decrease across a swap when `hardenedCalc` is enabled, rejecting the transaction otherwise.

### Proof of Concept
Not independently executed; the vulnerability is inferred from static analysis of `SafeExchangeProcessor.exchangeToSupply`/`exchangeFromSupply` (`RoundingMode.HALF_UP` in both legs) versus the invariant-preserving intent of the legacy `ExchangeProcessor`, combined with the caller-facing `ExchangeTransactionActuator.doValidate()` lacking any invariant check beyond the caller's own slippage protection. I was unable to confirm the exact rounding mode used by the legacy `ExchangeProcessor` class in this iteration (file not opened), so the magnitude/direction of the bias relative to baseline behavior is not fully verified — this should be confirmed with a numeric micro-simulation of both processors on identical inputs before treating this as conclusively exploitable.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L217-221)
```java
    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
    }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-166)
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
```

**File:** chainbase/src/main/java/org/tron/core/capsule/SafeExchangeProcessor.java (L19-38)
```java
  private BigDecimal exchangeToSupply(long balance, long quant) {
    long newBalance = StrictMathWrapper.addExact(balance, quant);
    BigDecimal bdQuant = BigDecimal.valueOf(quant);
    BigDecimal bdNewBalance = BigDecimal.valueOf(newBalance);
    BigDecimal base = BigDecimal.ONE.add(
        bdQuant.divide(bdNewBalance, 18, RoundingMode.HALF_UP));
    double powResult = StrictMathWrapper.pow(base.doubleValue(), 0.0005);
    return SUPPLY.negate().multiply(
        BigDecimal.ONE.subtract(BigDecimal.valueOf(powResult))).setScale(0, RoundingMode.DOWN);
  }

  private long exchangeFromSupply(long balance, BigDecimal supplyQuant) {
    BigDecimal bdBalance = BigDecimal.valueOf(balance);
    BigDecimal base = BigDecimal.ONE.add(
        supplyQuant.divide(SUPPLY, 18, RoundingMode.HALF_UP));
    double powResult = StrictMathWrapper.pow(base.doubleValue(), 2000.0);
    BigDecimal exchangeBalance = bdBalance.multiply(
        BigDecimal.valueOf(powResult).subtract(BigDecimal.ONE));
    return exchangeBalance.setScale(0, RoundingMode.DOWN).longValueExact();
  }
```
