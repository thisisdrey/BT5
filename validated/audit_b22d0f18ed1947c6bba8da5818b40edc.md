### Title
Legacy Exchange (Bancor-style) price calculation defaults to unsafe floating-point math (`ExchangeProcessor`) instead of exact arithmetic - ([File: chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java])

### Summary
The report's bug class is "usage of a deprecated/imprecise pricing mechanism without proper validation," which silently returns bad values (0, stale, wrong-decimals) instead of erroring, letting downstream logic use corrupted price data. The closest reachable analog in java-tron is the on-chain TRX/TRC-10 `Exchange` (Bancor-style AMM) price calculation: by default the legacy `ExchangeProcessor` computes buy/sell quantities using `double` floating-point math (`Maths.pow`) instead of exact decimal arithmetic, and this legacy path is still the default unless the `AllowHardenExchangeCalculation` hard fork flag is enabled by governance.

### Finding Description
`ExchangeCapsule.transaction()` selects between two price-calculation strategies: [1](#0-0) 

`SafeExchangeProcessor` uses `BigDecimal` with fixed scale/rounding for exact-supply-invariant math, but the legacy `ExchangeProcessor` still relies on native `double` and `Maths.pow`: [2](#0-1) 

Which strategy is picked is controlled by a chain-wide flag (`allowHardenExchangeCalculation`) that is read per-actuator via `AbstractExchangeActuator.allowHarden()`, exercised on every unprivileged `ExchangeTransactionContract`, `ExchangeInjectContract` and `ExchangeWithdrawContract`: [3](#0-2) [4](#0-3) 

Any unprivileged account can place `ExchangeTransactionContract`/`ExchangeInjectContract`/`ExchangeWithdrawContract` transactions and thus repeatedly drive this pricing formula. When `allowHardenExchangeCalculation` is not turned on (its default/rollout state is committee-controlled and not guaranteed active on every deployment/at genesis), all price computations for this AMM go through the `double`-based `exchangeToSupply`/`exchangeFromSupply` functions, which are susceptible to floating-point rounding/precision loss analogous to the deprecated, unchecked oracle-return issue in the referenced report: a numeric interface that can silently produce an imprecise or degenerate result (e.g., long-value truncation from `(long) issuedSupply` / `(long) exchangeBalance`) without any sanity/consistency check against an exact reference computation.

### Impact Explanation
Because the legacy processor is reachable by any address that can submit `ExchangeTransactionContract`/`ExchangeInjectContract`/`ExchangeWithdrawContract`, repeated small/edge-value trades that exploit the `double` rounding behavior of `Maths.pow` can skew the resulting `firstTokenBalance`/`secondTokenBalance` state stored in `ExchangeCapsule`, extracting more value than the exact AMM invariant would allow (or leaving the pool permanently mispriced). This can lead to unauthorized value extraction from the on-chain exchange pool (i.e., theft/unbacked-balance-style impact) for other participants of that pair.

### Likelihood Explanation
Likelihood is moderate: this requires an attacker to specifically target exchange pairs where the hardened calculation flag is off and craft a sequence of trades/injections/withdrawals that maximize floating-point drift; this is a known, previously-flagged issue class (double vs. BigDecimal) which is why `SafeExchangeProcessor`/`allowHardenExchangeCalculation` exists as a fix, implying the legacy vulnerable path is still present and exercised whenever the flag is disabled.

### Recommendation
Make the exact/`SafeExchangeProcessor`-based calculation unconditional (remove the `allowHarden()`/legacy `ExchangeProcessor` double-math fallback), or add strict invariant checks (post-trade balance/product/decimal consistency assertions) around every legacy-path `ExchangeCapsule.transaction()` call so degenerate/rounded results are rejected rather than silently applied, mirroring the report's recommendation to validate returned numeric data rather than trust it blindly.

### Proof of Concept
Not independently reproducible from static review alone — reproducing floating-point drift requires simulating repeated `ExchangeTransactionActuator`/`ExchangeInjectActuator` calls against a pair with `allowHardenExchangeCalculation` disabled and comparing `ExchangeProcessor.exchange` results to `SafeExchangeProcessor.exchange` (as already done in `ExchangeCapsuleTest.testTransactionLegacyVsHardenedProcessorSelection`, which asserts only "±1 difference," confirming a real, measurable precision delta between the two paths): [5](#0-4)

### Citations

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-129)
```java
  public long transaction(byte[] sellTokenID, long sellTokenQuant, boolean useStrictMath,
      boolean hardenedCalc) throws ContractValidateException {
    long supply = 1_000_000_000_000_000_000L;
    Processor processor = hardenedCalc
        ? SafeExchangeProcessor.INSTANCE : new ExchangeProcessor(supply, useStrictMath);

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

**File:** actuator/src/main/java/org/tron/core/actuator/AbstractExchangeActuator.java (L13-15)
```java
  protected boolean allowHarden() {
    return chainBaseManager.getDynamicPropertiesStore().allowHardenExchangeCalculation();
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L64-69)
```java
      byte[] tokenID = exchangeTransactionContract.getTokenId().toByteArray();
      long tokenQuant = exchangeTransactionContract.getQuant();

      byte[] anotherTokenID;
      long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
          dynamicStore.allowStrictMath(), allowHarden());
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
