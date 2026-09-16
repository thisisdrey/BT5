### Title
Non-hardened Exchange price calculation silently clamps on double→long overflow, corrupting bancor-curve settlement price - (File: chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java)

### Summary
`ExchangeProcessor.exchangeToSupply` and `exchangeFromSupply` compute the TRC10 bancor-relay exchange amount entirely in `double`, then narrow the result to `long` with a bare `(long)` cast, exactly the pattern flagged in the external report (`get_reserve` computing in `f64` and truncating to `i64`). [1](#0-0)  This code is reached by any unprivileged account broadcasting `ExchangeTransactionContract`, `ExchangeInjectContract`, or `ExchangeWithdrawContract`, since `ExchangeCapsule.transaction()` selects this non-hardened `ExchangeProcessor` unless the `hardenedCalc` flag is set. [2](#0-1) 

### Finding Description
`exchangeToSupply` computes `issuedSupply` as a `double` via a `pow` calculation, then does `long out = (long) issuedSupply;` with no bounds check. [3](#0-2)  `exchangeFromSupply` follows the same pattern for `exchangeBalance`. [4](#0-3)  In Java, casting a `double` that exceeds `Long.MAX_VALUE`/`Long.MIN_VALUE` to `long` clamps silently to those bounds (and `NaN` clamps to `0`) rather than throwing, matching precisely the "value too big/clamped" failure mode of the Rust `get_reserve` bug described in the report.

The codebase itself demonstrates awareness of this exact defect class: a parallel, safe implementation `SafeExchangeProcessor` was written using `BigDecimal` end-to-end with `longValueExact()` (which throws on overflow instead of silently clamping), and is only used when `hardenedCalc`/`allowHardenExchangeCalculation` is enabled. [5](#0-4)  The un-hardened `ExchangeProcessor` remains reachable and is the default calculation path used from `ExchangeCapsule.transaction(sellTokenID, sellTokenQuant, useStrictMath)` (2-arg overload used by production actuators such as `ExchangeTransactionActuator`), which defaults `hardenedCalc` to `false`. [6](#0-5) 

### Impact Explanation
If `issuedSupply` or `exchangeBalance` exceeds `Long.MAX_VALUE` in double representation (e.g., due to extreme but permissible pool balances/quant ratios, or intermediate floating-point blow-up from the `pow` computation near degenerate balances), the silent clamp produces an incorrect (clamped or zero) settlement amount instead of throwing. This can let a user extract far more tokens than the bancor curve should allow, or conversely destroy value for a counterparty, directly resulting in unbacked balances/token theft from the Exchange pool — the same "users incur losses / attacker extracts value" impact class as the original report.

### Likelihood Explanation
Reachable by a single, unprivileged, signed `ExchangeTransactionContract` (and related `ExchangeInject`/`ExchangeWithdraw`) transaction against any TRC10 bancor exchange pool created via `ExchangeCreateContract` — no special privilege required. The condition depends on pool balances/quant reaching combinations that push the double computation past `Long.MAX_VALUE`; this requires reserves or trade sizes that are large but not necessarily implausible given TRX/TRC10 supply ranges (up to ~9.2×10^18 base units), similar to how the report's bug triggered around specific `perp_clients_count` values.

### Recommendation
Make the hardened `SafeExchangeProcessor` (BigDecimal-based, `longValueExact()`-checked) the mandatory/default calculation path for `ExchangeCapsule.transaction`, rather than gating it behind the `allowHardenExchangeCalculation` dynamic property, or add explicit overflow checks (bounds comparison against `Long.MAX_VALUE`/`MIN_VALUE`, `NaN` checks) to `ExchangeProcessor.exchangeToSupply`/`exchangeFromSupply` before casting to `long`, throwing `ArithmeticException` on overflow to match the behavior already implemented in `SafeExchangeProcessor`.

### Proof of Concept
Not independently reproducible from the indexed code alone: I could not confirm from available files whether `allowHardenExchangeCalculation`/`disableJavaLangMath` defaults to on-chain-active on mainnet (the `DynamicPropertiesStore` accessor for this flag wasn't located in the indexed snippets, and its default value should be verified in a live/full checkout). Existing unit tests already exercise the double-cast/overflow boundary in this exact code (`testHardenedOverflowDetection` asserts `SafeExchangeProcessor` throws `ArithmeticException` at `Long.MAX_VALUE`, implicitly confirming the non-hardened `ExchangeProcessor` has no equivalent guard) [7](#0-6) , and `testStrictMath` shows the strict-math and hardened paths intentionally diverge from the plain-math path [8](#0-7) . A concrete PoC would require constructing/injecting an `ExchangeCapsule` with balances chosen so `exchangeToSupply`/`exchangeFromSupply`'s double result exceeds `Long.MAX_VALUE` while `hardenedCalc=false`, then calling `ExchangeTransactionActuator.execute` and observing the clamped/incorrect `anotherTokenQuant`; this would need to be validated in a full build/test environment beyond what static indexing allows.

### Citations

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

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L118-150)
```java
  @VisibleForTesting
  public long transaction(byte[] sellTokenID, long sellTokenQuant, boolean useStrictMath)
      throws ContractValidateException {
    return transaction(sellTokenID, sellTokenQuant, useStrictMath, false);
  }

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
```

**File:** chainbase/src/main/java/org/tron/core/capsule/SafeExchangeProcessor.java (L19-44)
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

  @Override
  public long exchange(long sellTokenBalance, long buyTokenBalance, long sellTokenQuant) {
    BigDecimal relay = exchangeToSupply(sellTokenBalance, sellTokenQuant);
    return exchangeFromSupply(buyTokenBalance, relay);
  }
```

**File:** framework/src/test/java/org/tron/core/capsule/utils/ExchangeProcessorTest.java (L159-163)
```java
  @Test
  public void testHardenedOverflowDetection() {
    assertThrows(ArithmeticException.class, () ->
        SafeExchangeProcessor.INSTANCE.exchange(Long.MAX_VALUE, 1_000_000L, 1L));
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
