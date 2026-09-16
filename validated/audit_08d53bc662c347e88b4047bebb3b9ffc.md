### Title
Non-hardened `ExchangeProcessor` uses floating-point Bancor math without invariant validation, allowing exchange pool reserves to go negative - (File: `chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java`)

### Summary
`calculateDistributeExcessIdleShareProceeds` in the referenced report uses an iterative numeric approximation (Newton's method) to solve for a value that must satisfy an invariant, and the fix required checking the result against a tolerance before trusting it (otherwise return `0`), with a hardened/safe path (`calculateDistributeExcessIdleShareProceedsNetLongEdgeCaseSafe`) added for correctness. Java-tron has the same structural pattern in its TRC10 Bancor `Exchange` mechanism: `ExchangeProcessor` computes trade output using `double`-precision floating point math (`Math.pow`), and the result is applied to update the pool reserves without verifying that the invariant (reserves must stay non-negative / consistent with the constant-product-like Bancor curve) holds — unless the chain has separately enabled a "hardened" calculation mode.

### Finding Description
`ExchangeCapsule.transaction()` computes trade output via a `Processor`, chosen based on a `hardenedCalc` flag: [1](#0-0) 

When `hardenedCalc` is `false` (the legacy path), `ExchangeProcessor` is used, which performs the Bancor-style relay calculation entirely in floating-point `double` arithmetic via `Maths.pow`: [2](#0-1) 

Critically, the resulting `buyTokenQuant` is used to update `newFirstTokenBalance`/`newSecondTokenBalance` via plain `long` arithmetic (`firstTokenBalance + sellTokenQuant`, `secondTokenBalance - buyTokenQuant`), and the non-negativity/invariant check is applied **only** when `hardenedCalc` is `true`: [3](#0-2) 

This mirrors the LPMath report exactly: an approximate numerical routine (floating-point `pow` instead of Newton's method) produces a value that is trusted and applied to update pool state without checking that the underlying invariant (reserve balances must remain non-negative and consistent with the bonding-curve relationship) actually holds. The "hardened" fix (`SafeExchangeProcessor`, `BigDecimal`-based, with an explicit non-negativity check) is only exercised when `chainBaseManager.getDynamicPropertiesStore().allowHardenExchangeCalculation()` is `true`: [4](#0-3) 

This is a committee-controlled dynamic parameter (toggled via `ProposalUtil`/`ProposalService`), meaning any network where that proposal has not (yet) been activated relies solely on the unchecked, floating-point legacy path for every `ExchangeTransactionActuator`, `ExchangeInjectActuator`, and `ExchangeWithdrawActuator` call — all of which are reachable from ordinary signed transactions by any account.

### Impact Explanation
If the floating-point `Math.pow`-based calculation in `ExchangeProcessor.exchangeFromSupply`/`exchangeToSupply` yields a `buyTokenQuant` larger than the actual `buyTokenBalance` (an edge case with extreme reserve ratios or large trade sizes, where `double` precision and truncation via `(long) exchangeBalance` diverge from the true Bancor curve), the pool's `secondTokenBalance` (or `firstTokenBalance`) can go negative without being rejected, since the non-negativity check in `ExchangeCapsule.transaction()` is gated by `hardenedCalc`. A corrupted, negative-appearing (or overflowed) reserve breaks the AMM's core solvency invariant, letting a trader extract more value than the pool actually holds — an unbacked-balance / fund-drain condition for subsequent traders and pool participants (`ExchangeWithdrawActuator`, further `ExchangeTransactionActuator` calls) that read from the corrupted `ExchangeCapsule` state.

### Likelihood Explanation
Reachability is trivial: any account can call `ExchangeTransactionActuator` (a standard broadcastable contract type) against any existing TRC10 `Exchange` pool with a chosen `quant`. Likelihood of actually triggering an unsafe result depends on whether `AllowHardenExchangeCalculation` has been activated on the target network/chain (via committee proposal) and on finding reserve ratios/trade sizes where `double` rounding diverges enough from the exact Bancor formula to push `buyTokenQuant` past the true reserve. This requires numeric analysis to identify exact triggering inputs, similar to the original report where a similar Newton's-method-based edge case required specific construction to break the invariant — plausible but not trivially demonstrated without further off-chain computation.

### Recommendation
Apply the same remediation pattern the Spearbit/Delv fix used for LPMath: always validate the invariant (non-negative, economically consistent reserves) after computing the trade output — not just in the `hardenedCalc` branch — and reject the transaction if the check fails, regardless of whether "hardened" mode has been enabled by governance. Consider making the `SafeExchangeProcessor` (BigDecimal-based) computation the default and unconditional path rather than opt-in.

### Proof of Concept
Full exploitation requires constructing specific `firstTokenBalance`/`secondTokenBalance`/`sellTokenQuant` inputs where `ExchangeProcessor`'s double-precision `Math.pow` computation diverges enough from the exact value to produce a `buyTokenQuant` exceeding the actual reserve, and confirming the target network has not activated `AllowHardenExchangeCalculation`. This numeric edge-case search was not completed within the scope of this analysis; the existing test suite (`ExchangeProcessorTest`, `testHardenedLargeQuant`, `testSafeProcessorNoOvershootForTypicalInputs`) explicitly documents that the safe/hardened processor was added specifically to guard against overshoot for realistic inputs, which supports that the legacy non-hardened path is the vulnerable analog: [5](#0-4)

### Citations

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-146)
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

```

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L158-166)
```java
    }

    if (hardenedCalc && (newFirstTokenBalance < 0 || newSecondTokenBalance < 0)) {
      throw new ContractValidateException("Exchange balance must be >=0 after transaction");
    }
    this.exchange = this.exchange.toBuilder()
        .setFirstTokenBalance(newFirstTokenBalance)
        .setSecondTokenBalance(newSecondTokenBalance)
        .build();
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
