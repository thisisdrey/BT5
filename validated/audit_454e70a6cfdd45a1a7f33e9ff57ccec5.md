Based on my research, the relevant analog in java-tron is in the **TRC10 Bancor-style Exchange module** (`ExchangeInjectActuator`, `ExchangeWithdrawActuator`, `ExchangeTransactionActuator`), which acts as an on-chain AMM. All arithmetic-safety guarantees for this AMM are gated behind a dynamic-property flag (`allowHardenExchangeCalculation`) that must be turned on by a committee proposal; when it is off (its un-activated / pre-proposal state), the code silently falls back to unchecked arithmetic.

### Title
Exchange balance-limit and swap-invariant checks are bypassable via unchecked long overflow when `allowHardenExchangeCalculation` is disabled - (File: `actuator/src/main/java/org/tron/core/actuator/AbstractExchangeActuator.java`)

### Summary
`AbstractExchangeActuator.addExact`/`subtractExact` only perform overflow-checked arithmetic when the dynamic property `allowHardenExchangeCalculation` is enabled; otherwise they silently use plain `+`/`-`. [1](#0-0) 
The same conditional behavior propagates into `ExchangeCapsule.transaction`, where pool balance updates use unchecked `firstTokenBalance + sellTokenQuant` / `secondTokenBalance - buyTokenQuant` unless `hardenedCalc` is true. [2](#0-1) 
It also propagates into the swap-price math itself: the default `ExchangeProcessor` computes the bancor-style relay/return values using `double` arithmetic with no bounds checking, while the alternate `SafeExchangeProcessor` uses `BigDecimal`/`StrictMathWrapper` with explicit overflow detection. [3](#0-2) [4](#0-3) 

### Finding Description
`ExchangeInjectActuator.doValidate()` computes `tokenBalance = addExact(tokenBalance, tokenQuant)` and rejects the transaction only if the result exceeds `dynamicStore.getExchangeBalanceLimit()`. [5](#0-4) 
Because `addExact` here is the actuator's own method — not `Math.addExact` — this "limit" check silently degrades to unchecked `long` addition when the harden flag is off. A crafted `quant` in an attacker-signed `ExchangeInjectContract`/`ExchangeTransactionContract`/`ExchangeWithdrawContract` (the `quant`, `token_id`, `exchange_id` fields are fully attacker-controlled per the protobuf spec) can push `tokenBalance` past `Long.MAX_VALUE`, wrapping to a negative value that trivially satisfies `tokenBalance > balanceLimit == false`, defeating the intended cap.
The actual pool-state mutation then happens through `ExchangeCapsule.transaction`/`setBalance`, again performed with unchecked long math and, for swaps, with `double`-based bancor pricing that has no invariant/overshoot protection in the non-hardened path (contrast with `SafeExchangeProcessor`'s explicit `result <= buyBalance` guarantee validated only in its own test suite). [6](#0-5) 
This is directly analogous to the FLOKI incident's root cause: an AMM's balance/price accounting can be pushed into an inconsistent state by a single attacker-supplied large value, letting the attacker extract more of the paired asset than the pool actually holds or than the invariant should allow.

### Impact Explanation
If reached with `allowHardenExchangeCalculation` disabled (its default/pre-activation state), an attacker with a signed `ExchangeInjectContract`, `ExchangeWithdrawContract`, or `ExchangeTransactionContract` could overflow the `long` balance checks, bypass the configured `ExchangeBalanceLimit`, corrupt the AMM pool's `first_token_balance`/`second_token_balance`, and drain paired TRX/TRC10 assets held by the Exchange pool — i.e., theft/unbacked-balance impact on funds held in `ExchangeStore`/`ExchangeV2Store`.

### Likelihood Explanation
Reachability requires only a normal signed transaction from any account holding the tokens/TRX needed to invoke `ExchangeInject`/`ExchangeTransaction`/`ExchangeWithdraw` — no privileged role is needed. However, exploitability is conditioned on the network **not** having enabled `allowHardenExchangeCalculation` (a committee-controlled dynamic property whose current on-chain/default value I could not confirm from the index). If the hardening proposal has already been activated network-wide, this path is not exploitable, which materially lowers likelihood and is a significant caveat on this finding.

### Recommendation
Make `addExact`/`subtractExact` in `AbstractExchangeActuator`, and the balance math in `ExchangeCapsule.transaction`, unconditionally use overflow-checked arithmetic (`Math.addExact`/`StrictMathWrapper`) regardless of the `allowHardenExchangeCalculation` flag, and route all swap pricing exclusively through `SafeExchangeProcessor`'s invariant-checked math. Treat the "hardened" path as the only path rather than an optional, proposal-gated one.

### Proof of Concept
Not independently reproducible from the indexed code alone: I could not confirm the current/default value of `allowHardenExchangeCalculation` in `DynamicPropertiesStore`, nor find a runnable end-to-end scenario proving actual `long` overflow given real `ExchangeBalanceLimit` values (these are typically far below `Long.MAX_VALUE`, so many injections/transactions might be needed to actually overflow rather than a single transaction). Given index size limits, I was not able to fully inspect `DynamicPropertiesStore.allowHardenExchangeCalculation()` and its default value, or the full `MarketSellAssetActuator` matching logic, to construct a concrete numeric PoC. I recommend a Devin session with full repo/DB access to verify the default flag state and construct a concrete overflow/precision-loss transaction sequence before treating this as confirmed-exploitable.

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

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L136-158)
```java
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L199-205)
```java
    long balanceLimit = dynamicStore.getExchangeBalanceLimit();
    long tokenBalance = (Arrays.equals(tokenID, firstTokenID) ? firstTokenBalance
        : secondTokenBalance);
    tokenBalance = addExact(tokenBalance, tokenQuant);
    if (tokenBalance > balanceLimit) {
      throw new ContractValidateException("token balance must less than " + balanceLimit);
    }
```

**File:** framework/src/test/java/org/tron/core/capsule/utils/ExchangeProcessorTest.java (L201-216)
```java
  @Test
  public void testSafeProcessorNoOvershootForTypicalInputs() {
    // Verify across realistic inputs that hardened result never exceeds buy reserve.
    long[][] data = {
        {100_000_000L, 100_000_000L, 1_000_000L},
        {1_000_000_000L, 1_000_000_000L, 100_000L},
        {1L, 10_140_000_000_000L, 2_897_000_000_000L},
        {903L, 737L, 50L},
    };
    for (long[] row : data) {
      long result = SafeExchangeProcessor.INSTANCE.exchange(row[0], row[1], row[2]);
      Assert.assertTrue("Result must be non-negative", result >= 0);
      Assert.assertTrue("Result must not exceed buy reserve, got " + result + " > " + row[1],
          result <= row[1]);
    }
  }
```
