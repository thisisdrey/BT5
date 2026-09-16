### Title
Unguarded long-arithmetic overflow/underflow in Bancor-style Exchange (TRC10) price calculation defaults to unsafe path - ([File: chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java])

### Summary
The TRC10 Exchange/Bancor pool math in java-tron mirrors the reported Balancer/Aura oracle bug class: unsafe, unchecked arithmetic used to compute exchanged token amounts from pool reserves. `ExchangeInjectActuator`, `ExchangeTransactionActuator`, and `ExchangeWithdrawActuator` all route the actual reserve mutation through helper methods `addExact`/`subtractExact` in `AbstractExchangeActuator`, which only perform overflow-checked math when a chain-wide dynamic property (`allowHardenExchangeCalculation`) is enabled; otherwise they fall back to raw `x + y` / `x - y`, and the Bancor-curve computation itself (`ExchangeProcessor.exchangeToSupply`/`exchangeFromSupply`) uses plain `double` math with no bounds checking at all.

### Finding Description
`AbstractExchangeActuator.addExact`/`subtractExact` gate safe math behind `allowHarden()`: [1](#0-0) 

When the hardened flag is off (the default/legacy code path still shipped and reachable by any unprivileged caller submitting an `ExchangeInjectContract`, `ExchangeTransactionContract`, or `ExchangeWithdrawContract`), reserve balance updates use unchecked `long` addition/subtraction that silently wraps on overflow/underflow instead of throwing, e.g. in `ExchangeInjectActuator.execute`: [2](#0-1) 

The actual price/quantity computation in the default (non-hardened) path, `ExchangeProcessor.exchangeToSupply`/`exchangeFromSupply`, performs the Bancor formula purely in `double` precision with no validation that `newBalance`, `supply`, or intermediate ratios stay within safe bounds — directly analogous to the reported `ratio`/`fairResA` computation in `BalancerPairOracle` that could divide by zero or overflow when reserves are skewed: [3](#0-2) 

By contrast, the "hardened" path (`SafeExchangeProcessor`) fixes exactly this class of bug by using `BigDecimal`/`StrictMathWrapper.addExact` and rejecting negative resulting balances: [4](#0-3) [5](#0-4) 

This confirms the underlying math is fragile without safe-math guards — the codebase's own fix acknowledges the bug class exists — but the guard is opt-in via a dynamic property rather than being the only code path, so any deployment/chain state where `allowHardenExchangeCalculation` has not been activated by committee proposal remains exposed to the unguarded arithmetic that any account can trigger with an ordinary transaction.

### Impact Explanation
Any account can call `ExchangeTransactionActuator`/`ExchangeInjectActuator`/`ExchangeWithdrawActuator` against an existing TRC10 Exchange pair. On the unguarded path, `long` overflow/underflow in `firstTokenBalance`/`secondTokenBalance` updates or a skewed/adversarial reserve ratio driving the `double`-based Bancor formula to extreme values could silently produce an incorrect `anotherTokenQuant`, corrupting the pool's on-chain reserve balances (`ExchangeCapsule`) or crediting/debiting the wrong TRX/TRC10 amount to the caller's account — i.e., unbacked balance creation or permanent freezing/loss of pooled funds, matching the "unbacked balance"/"theft of funds" impact bar.

### Likelihood Explanation
Reachability requires only a normal signed transaction against a TRC10 Exchange (no special privilege), so likelihood is high wherever `allowHardenExchangeCalculation` is not enabled. Exact triggering conditions (specific reserve ratios/sell quantities needed to force an overflow or a materially wrong result rather than a revert) were not empirically re-derived here beyond the code-level construction shown above, so the precise numeric example is unproven — this is a code-level structural analog rather than a verified live exploit.

### Recommendation
Make the safe-math path (`SafeExchangeProcessor`/`StrictMathWrapper` based `addExact`/`subtractExact`) unconditional for all Exchange actuators rather than gated behind `allowHardenExchangeCalculation`, and add explicit bounds/sanity checks (non-zero denominators, resulting balances ≥ 0, reasonable precision loss bounds) around the Bancor formula regardless of the flag, consistent with the recommendation to add precision-controlled fixed-point math cited in the original report.

### Proof of Concept
Not independently reproduced with concrete numeric inputs in this pass; the structural proof is the divergence between the legacy unguarded path (`ExchangeProcessor` + raw `+`/`-` in `AbstractExchangeActuator`) and the hardened path (`SafeExchangeProcessor` + `StrictMathWrapper`), which exists specifically because the unguarded path can overflow/underflow or lose precision, as shown by the repository's own tests validating overflow detection only for the hardened processor: [6](#0-5)

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/AbstractExchangeActuator.java (L13-23)
```java
  protected boolean allowHarden() {
    return chainBaseManager.getDynamicPropertiesStore().allowHardenExchangeCalculation();
  }

  public long subtractExact(long x, long y) {
    return allowHarden() ? StrictMathWrapper.subtractExact(x, y) : x - y;
  }

  public long addExact(long x, long y) {
    return allowHarden() ? StrictMathWrapper.addExact(x, y) : x + y;
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L71-83)
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

**File:** framework/src/test/java/org/tron/core/capsule/utils/ExchangeProcessorTest.java (L159-199)
```java
  @Test
  public void testHardenedOverflowDetection() {
    assertThrows(ArithmeticException.class, () ->
        SafeExchangeProcessor.INSTANCE.exchange(Long.MAX_VALUE, 1_000_000L, 1L));
  }

  @Test
  public void testHardenedSmallQuant() {

    long sellBalance = 1_000_000_000_000_000L;
    long buyBalance = 1_000_000_000_000_000L;
    long sellQuant = 1L;

    long result = SafeExchangeProcessor.INSTANCE.exchange(sellBalance, buyBalance, sellQuant);
    Assert.assertTrue("Result must be non-negative for small quant", result >= 0);
  }

  @Test
  public void testHardenedLargeQuant() {
    long sellBalance = 1_000_000_000_000L;
    long buyBalance = 1_000_000_000_000L;
    long sellQuant = 1_000_000_000_000L; // 100% of sell balance

    long result = SafeExchangeProcessor.INSTANCE.exchange(sellBalance, buyBalance, sellQuant);
    Assert.assertTrue("Result must be positive for large quant", result > 0);
    Assert.assertTrue("Result must be less than buy balance", result < buyBalance);
  }

  @Test
  public void testSafeProcessorDivByZeroThrows() {
    // newBalance = balance + quant = -1 + 1 = 0 -> BigDecimal divide by zero
    assertThrows(ArithmeticException.class,
        () -> SafeExchangeProcessor.INSTANCE.exchange(-1L, 100L, 1L));
  }

  @Test
  public void testSafeProcessorAddExactOverflowThrows() {
    // balance + quant = MAX + 1 -> addExact overflow
    assertThrows(ArithmeticException.class,
        () -> SafeExchangeProcessor.INSTANCE.exchange(Long.MAX_VALUE, 1L, 1L));
  }
```
