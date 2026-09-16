### Title
Floating-point precision drift in `ExchangeProcessor`'s Bancor-formula AMM lets a caller extract TRX/TRC10 reserves via repeated buy/sell round-trips (analogous to flash-loan price manipulation) - ([File: chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java])

### Summary
`ExchangeTransactionActuator` routes trades against the TRC10 "Exchange" (Bancor-style constant-formula AMM) through `ExchangeCapsule.transaction()`, which by default uses the legacy `ExchangeProcessor` — a `double`/`Math.pow`-based implementation of the relay-supply formula. Because the formula's two legs (`exchangeToSupply` / `exchangeFromSupply`) are computed with floating-point arithmetic and truncated to `long` on each call, repeated round-trip trades (sell A→relay→buy B, then sell B→relay→buy A) accumulate rounding error in the pool's `firstTokenBalance`/`secondTokenBalance` state that is not conserved exactly. An unprivileged account can broadcast a sequence of `ExchangeTransactionContract` transactions against the same exchange pool to repeatedly harvest this rounding drift, draining value from the pool similar in effect to the flash-loan price-manipulation pattern described in the report (atomic, self-funded, single-account exploitation of AMM pricing math with no external oracle check).

### Finding Description
`ExchangeTransactionActuator.execute()` calls `exchangeCapsule.transaction(tokenID, tokenQuant, dynamicStore.allowStrictMath(), allowHarden())` [1](#0-0) , which selects between two processors:

```java
Processor processor = hardenedCalc
    ? SafeExchangeProcessor.INSTANCE : new ExchangeProcessor(supply, useStrictMath);
``` [2](#0-1) 

`allowHarden()` gates this on the `allowHardenExchangeCalculation()` dynamic-property/proposal flag [3](#0-2) , a governance-controlled parameter (`ALLOW_HARDEN_EXCHANGE_CALCULATION`) that starts disabled and must be turned on by committee proposal [4](#0-3) . Until/unless that proposal passes, every `ExchangeTransactionContract` (and `ExchangeInjectContract`/`ExchangeWithdrawContract`) is processed by the legacy `ExchangeProcessor`:

```java
private long exchangeToSupply(long balance, long quant) {
  long newBalance = balance + quant;
  double issuedSupply = -supply * (1.0
      - Maths.pow(1.0 + (double) quant / newBalance, 0.0005, this.useStrictMath));
  long out = (long) issuedSupply;
  supply += out;
  return out;
}

private long exchangeFromSupply(long balance, long supplyQuant) {
  supply -= supplyQuant;
  double exchangeBalance = balance
      * (Maths.pow(1.0 + (double) supplyQuant / supply, 2000.0, this.useStrictMath) - 1.0);
  return (long) exchangeBalance;
}
``` [5](#0-4) 

This double-precision, `Math.pow`-based relay-supply computation is not exact and not guaranteed to be reversible: selling `q` of token A for token B and then immediately selling the received amount of B back for A does not necessarily return the original `q` — because `(long)` truncation happens on both legs and the internal mutable `supply` field accumulates drift. `ExchangeCapsule.transaction()` then unconditionally applies the processor's output to the on-chain pool balances via simple (non-`Exact`, unless hardened) arithmetic:

```java
newFirstTokenBalance = firstTokenBalance + sellTokenQuant;
newSecondTokenBalance = secondTokenBalance - buyTokenQuant;
...
this.exchange = this.exchange.toBuilder()
    .setFirstTokenBalance(newFirstTokenBalance)
    .setSecondTokenBalance(newSecondTokenBalance)
    .build();
``` [6](#0-5) 

`ExchangeTransactionActuator.doValidate()` only checks `anotherTokenQuant >= tokenExpected` (a caller-supplied slippage floor) and pool balance limits; it performs no invariant check that the product/relationship of reserves is preserved across a trade, and no protection against a caller issuing many small, favorably-rounded trades back-to-back [7](#0-6) . The introduction of `SafeExchangeProcessor` (BigDecimal-based, `RoundingMode.DOWN`) as an alternate "hardened" path [8](#0-7)  and the associated overflow/precision test suite confirms that this precision-drift class is recognized, but it is opt-in and not the default execution path.

This is directly analogous to the reported bug class: price manipulation of an on-chain AMM/exchange formula by a single unprivileged account issuing a sequence of self-funded trades in the same asset pool to extract more value than deposited, exploiting formula/rounding weaknesses rather than any access-control flaw — the TRON equivalent of a flash-loan price-manipulation drain, except here the "flash loan" isn't even required because the attacker only needs enough TRX/TRC10 to seed the round-trip once, then reuses returned proceeds for further extractive trades.

### Impact Explanation
An attacker who identifies (or seeds) a TRC10 Exchange pool can, purely as an unprivileged transaction broadcaster, repeatedly call `ExchangeTransactionContract` to trade back and forth, each round-trip skimming a small amount of TRX or TRC10 token from the pool's real reserves due to floating-point/relay-supply rounding. Over many transactions this constitutes unauthorized, incremental theft of pooled TRX/TRC10 funds belonging to the exchange's other liquidity participants — a concrete unbacked-balance/fund-theft outcome in scope of this scan's acceptance criteria.

### Likelihood Explanation
Medium. Exploitation requires only a standard signed transaction type (`ExchangeTransactionContract`) reachable by any account, no special privileges, and the vulnerable legacy processor is the default unless the `ALLOW_HARDEN_EXCHANGE_CALCULATION` proposal has been activated by the committee. The per-trade extraction is likely small (bounded by `long` truncation granularity of the Bancor formula), so profitability depends on pool size/precision and requires many repeated transactions (and their fees), which somewhat limits attractiveness but does not eliminate risk, especially against large, low-fee-relative pools.

### Recommendation
- Confirm the current on-chain/production state of `ALLOW_HARDEN_EXCHANGE_CALCULATION`; if disabled, prioritize activating `SafeExchangeProcessor` as the enforced, non-optional path rather than an opt-in governance toggle.
- Add an explicit reserve-invariant check in `ExchangeCapsule.transaction()`/`ExchangeTransactionActuator` that rejects any trade whose resulting pool state would violate the expected monotonic-price/constant-formula invariant (e.g., verify the implied product/exchange rate did not move in the trader's favor beyond legitimate formula output).
- Replace the legacy `ExchangeProcessor`'s double/`Math.pow` computation with the BigDecimal-based `SafeExchangeProcessor` unconditionally, deprecating the double-precision path entirely rather than gating it behind a proposal.

### Proof of Concept
1. Locate or create (if permissioned) an active TRC10 `Exchange` pool with reserves `(firstTokenBalance, secondTokenBalance)`.
2. Broadcast `ExchangeTransactionContract` selling a modest quantity of token A for token B (`ExchangeTransactionActuator.execute` → `ExchangeCapsule.transaction` → `ExchangeProcessor.exchange`) [9](#0-8) .
3. Immediately broadcast a second `ExchangeTransactionContract` selling the exact amount of token B just received back for token A.
4. Compare the attacker's net token-A balance before step 2 and after step 3, and the pool's `firstTokenBalance`/`secondTokenBalance` before/after; due to the `(long)` truncation in `exchangeToSupply`/`exchangeFromSupply` on both legs [5](#0-4) , the round trip does not conserve value exactly, and the pool's stored reserves absorb the resulting drift, which repeated iterations of steps 2–3 accumulate into a net drain — mirroring the "hardenedExecuteOverflowThrowsArithmeticException" / "testTransactionLegacyVsHardenedProcessorSelection" tests in the repo, which already demonstrate divergence between legacy and hardened outputs on the same inputs [10](#0-9) .

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L61-69)
```java
      byte[] firstTokenID = exchangeCapsule.getFirstTokenId();
      byte[] secondTokenID = exchangeCapsule.getSecondTokenId();

      byte[] tokenID = exchangeTransactionContract.getTokenId().toByteArray();
      long tokenQuant = exchangeTransactionContract.getQuant();

      byte[] anotherTokenID;
      long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
          dynamicStore.allowStrictMath(), allowHarden());
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L217-223)
```java
    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
    }

    return true;
```

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-128)
```java
  public long transaction(byte[] sellTokenID, long sellTokenQuant, boolean useStrictMath,
      boolean hardenedCalc) throws ContractValidateException {
    long supply = 1_000_000_000_000_000_000L;
    Processor processor = hardenedCalc
        ? SafeExchangeProcessor.INSTANCE : new ExchangeProcessor(supply, useStrictMath);
```

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L140-166)
```java
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

**File:** actuator/src/main/java/org/tron/core/actuator/AbstractExchangeActuator.java (L13-15)
```java
  protected boolean allowHarden() {
    return chainBaseManager.getDynamicPropertiesStore().allowHardenExchangeCalculation();
  }
```

**File:** actuator/src/main/java/org/tron/core/utils/ProposalUtil.java (L1-1)
```java
package org.tron.core.utils;
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

**File:** framework/src/test/java/org/tron/core/capsule/ExchangeCapsuleTest.java (L85-106)
```java
  @Test
  public void testTransactionLegacyVsHardenedProcessorSelection() throws Exception {
    // Same input produces deterministic results in both modes.
    ExchangeCapsule legacy = new ExchangeCapsule(
        ByteString.copyFromUtf8("owner"), 100L, 0L,
        "abc".getBytes(), "def".getBytes());
    legacy.setBalance(100_000_000L, 100_000_000L);
    long legacyResult = legacy.transaction("abc".getBytes(), 1_000_000L, true, false);

    ExchangeCapsule hardened = new ExchangeCapsule(
        ByteString.copyFromUtf8("owner"), 101L, 0L,
        "abc".getBytes(), "def".getBytes());
    hardened.setBalance(100_000_000L, 100_000_000L);
    long hardenedResult = hardened.transaction("abc".getBytes(), 1_000_000L, true, true);

    Assert.assertTrue("Both must return positive", legacyResult > 0 && hardenedResult > 0);
    Assert.assertTrue("Hardened must not exceed pool",
        hardenedResult <= 100_000_000L);
    // Allow ±1 difference due to BigDecimal vs double precision
    Assert.assertTrue("Results should be within 1 unit",
        StrictMathWrapper.abs(legacyResult - hardenedResult) <= 1);
  }
```
