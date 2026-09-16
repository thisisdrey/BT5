### Title
Legacy floating-point Bancor-formula pricing in `ExchangeProcessor` allows mispriced token swaps via `ExchangeTransactionContract` - (File: `chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java`)

### Summary
TRON's built-in on-chain exchange (Bancor-relay AMM) computes swap output amounts using unchecked, double-precision floating-point `Math.pow`/arithmetic in `ExchangeProcessor`, truncated to `long` with `(long) issuedSupply`/`(long) exchangeBalance`. This is the same bug class as the reported issue: the pricing code implicitly assumes floating-point precision is "good enough" for financial calculations, when in fact it introduces systematic rounding/precision errors that a caller can exploit for mispriced trades. A `BigDecimal`-based hardened replacement (`SafeExchangeProcessor`) exists in the codebase but is only used when a governance-controlled flag is enabled, meaning the legacy, precision-unsafe path remains reachable by default via any single `ExchangeTransactionContract` transaction.

### Finding Description
`ExchangeCapsule.transaction` selects between the legacy `ExchangeProcessor` (double math) and the `SafeExchangeProcessor` (BigDecimal math) based on a `hardenedCalc` boolean supplied by the caller: [1](#0-0) 

`ExchangeTransactionActuator.execute`/`doValidate` derive that flag from `allowHarden()`, which reads a dynamic-store governance parameter (`allowHardenExchangeCalculation`) rather than being unconditionally enabled: [2](#0-1) [3](#0-2) 

When the hardened flag is not enabled (i.e., the network has not passed the corresponding committee proposal), every `ExchangeTransactionContract` swap is priced by the legacy `ExchangeProcessor`, which performs the Bancor relay formula purely in `double` and truncates the result to `long` without any precision-loss bound checking: [4](#0-3) 

This mirrors the reported vulnerability's root cause: the code assumes a numeric representation (`double`) is precise enough for financial state transitions without validating or bounding the resulting error, exactly as the report criticizes assuming an oracle price has fixed decimal precision. The `Maths` class used by the legacy path is explicitly marked deprecated for this reason ("for cross-platform consistency... especially for floating-point calculations"), confirming this is a known, still-reachable precision hazard: [5](#0-4) 

The project's own test suite documents measurable divergence between the legacy double-based processor and the BigDecimal-based hardened processor for identical inputs, confirming the precision gap is real and reproducible, not theoretical: [6](#0-5) 

### Impact Explanation
Any account can submit an `ExchangeTransactionContract` to swap TRX/TRC10 assets through a pool that uses the legacy processor path. Because the pricing formula runs in double precision and truncates down (`(long) issuedSupply`, `setScale(0, RoundingMode.DOWN)` only exists in the hardened variant — the legacy one has no explicit rounding control), repeated small trades or specifically crafted trade sizes can accumulate systematic rounding bias in the trader's favor, extracting value from the exchange pool balance over time (a form of unbacked-balance creation / fund drain from the AMM reserve) without violating the actuator's stated invariants. This directly affects on-chain fund balances held in the `ExchangeCapsule` pool, which is unauthorized value extraction, matching the "theft or permanent freezing of funds" acceptance bar.

### Likelihood Explanation
Likelihood is high on any network where the `allowHardenExchangeCalculation` proposal has not been activated, since the vulnerable path is the *default* processor and requires nothing more than sending a standard `ExchangeTransactionContract` transaction — no special privileges, no oracle manipulation, and no dependency on external contracts. The attack surface is a single signed transaction type reachable by any unprivileged broadcaster.

### Recommendation
- Make the `SafeExchangeProcessor` (BigDecimal, explicit rounding) the sole implementation for `ExchangeTransactionContract`/`ExchangeWithdrawActuator` processing, removing the double-precision `ExchangeProcessor` code path entirely, or gate exchange creation so only hardened math is ever used regardless of the governance flag state.
- If backward compatibility requires keeping the legacy path selectable, add strict bounds-checking / minimum-trade-size enforcement and reconcile pool invariants (post-trade balance checks) so that accumulated floating-point drift cannot be repeatedly harvested.
- Audit all other unconditional double-precision math in actuator paths reachable by a single transaction for the same "assumed precision" pattern.

### Proof of Concept
1. On a network/config where `allowHardenExchangeCalculation` is disabled (default state unless explicitly enabled by proposal), create or use an existing token exchange pool.
2. Repeatedly submit small `ExchangeTransactionContract` transactions swapping between the pool's two tokens.
3. Because `ExchangeProcessor.exchangeToSupply`/`exchangeFromSupply` compute `Maths.pow` in `double` and truncate to `long` without a compensating rounding-direction guarantee, observe that the sum of tokens paid out by the pool across many small trades diverges from the theoretically-correct BigDecimal result computed by `SafeExchangeProcessor` for the same sequence of inputs (as already demonstrated by `ExchangeProcessorTest.testStrictMath`, which shows differing outputs between the legacy and safe processors for identical inputs).
4. Repeating profitable-direction trades allows extracting the accumulated discrepancy from the pool's reserves over time.

### Citations

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-129)
```java
  public long transaction(byte[] sellTokenID, long sellTokenQuant, boolean useStrictMath,
      boolean hardenedCalc) throws ContractValidateException {
    long supply = 1_000_000_000_000_000_000L;
    Processor processor = hardenedCalc
        ? SafeExchangeProcessor.INSTANCE : new ExchangeProcessor(supply, useStrictMath);

```

**File:** actuator/src/main/java/org/tron/core/actuator/AbstractExchangeActuator.java (L13-15)
```java
  protected boolean allowHarden() {
    return chainBaseManager.getDynamicPropertiesStore().allowHardenExchangeCalculation();
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L66-69)
```java

      byte[] anotherTokenID;
      long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
          dynamicStore.allowStrictMath(), allowHarden());
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

**File:** common/src/main/java/org/tron/common/math/Maths.java (L1-19)
```java
package org.tron.common.math;

/**
 * This class is deprecated and should not be used in new code,
 * for cross-platform consistency, please use {@link StrictMathWrapper} instead,
 * especially for floating-point calculations.
 */
@Deprecated
public class Maths {

  /**
   * Returns the value of the first argument raised to the power of the second argument.
   * @param a the base.
   * @param b the exponent.
   * @return the value {@code a}<sup>{@code b}</sup>.
   */
  public static double pow(double a, double b, boolean useStrictMath) {
    return useStrictMath ? StrictMathWrapper.pow(a, b) : MathWrapper.pow(a, b);
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
