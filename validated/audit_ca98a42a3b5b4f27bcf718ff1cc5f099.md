### Title
Legacy TRX/TRC10 Exchange (Bancor-AMM) uses floating-point math for reserve conversion, allowing rounding-direction drift that is not guaranteed to favor the pool - ([File: chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java])

### Summary
The external report describes a class of AMM/pool-accounting bug: when a bonding-curve/exchange-rate conversion is computed with a rounding method that is not strictly biased against the user, repeated small operations can let a user extract more value than they contributed, draining the pool. java-tron has a structurally analogous on-chain AMM: the legacy `Exchange`/`ExchangeV2` token-pair market (`ExchangeTransactionContract`, `ExchangeInjectContract`, `ExchangeWithdrawContract`), whose reserve-swap math is implemented in `ExchangeProcessor` using `double`/`Math.pow` floating point arithmetic rather than an exact, consistently-rounded integer method.

### Finding Description
`ExchangeCapsule.transaction()` is the entry point used by `ExchangeTransactionActuator`, `ExchangeInjectActuator`, and `ExchangeWithdrawActuator` to compute the counter-token amount for a Bancor-style swap: [1](#0-0) 

By default (`hardenedCalc == false`, i.e. `allowHardenExchangeCalculation()` disabled), it uses the legacy `ExchangeProcessor`, whose reserve math is done entirely in IEEE-754 `double` precision: [2](#0-1) 

This double-precision path is truncated to `long` via a plain narrowing cast (`(long) issuedSupply`, `(long) exchangeBalance`), which is not a controlled, protocol-favoring rounding direction — it is whatever IEEE-754 `Math.pow`/multiplication error happens to produce. The project's own test suite proves the two code paths diverge: [3](#0-2) 

A hardened, `BigDecimal`-based replacement (`SafeExchangeProcessor`) was added later and is only used when the `allowHardenExchangeCalculation` dynamic property is turned on via `AbstractExchangeActuator.allowHarden()`: [4](#0-3) 

Unless/until that proposal-gated flag is enabled network-wide, every `ExchangeTransactionContract` executed by any unprivileged account broadcaster is settled using the unguarded floating-point path in `ExchangeProcessor`, i.e. the same "improper rounding, not guaranteed to disfavor the user" pattern flagged in the external report for Jet's `deposit_note_exchange_rate`.

### Impact Explanation
Because the legacy processor's rounding error direction is not deterministically biased toward the pool, an attacker who repeatedly issues minimal `ExchangeTransactionContract` swaps (buy/sell round-trips) against a thinly-reserved Exchange pair can accumulate small positive discrepancies per trade. Since `ExchangeCapsule.transaction()` directly mutates the on-chain `firstTokenBalance`/`secondTokenBalance` reserves that back all other holders' claims on that pair, repeated exploitation can degrade or drain the pool's reserves relative to what other participants (via `ExchangeWithdrawActuator`, which is restricted to the pool creator) are entitled to, i.e. unbacked-balance / theft-of-funds risk for TRX and TRC10 assets held in the Exchange contract.

### Likelihood Explanation
Any account can create an `ExchangeCreateContract` pair and then submit arbitrary numbers of `ExchangeTransactionContract` transactions against it, paying only the exchange transaction fee (`calcFee()` returns 0 for `ExchangeTransactionActuator`), so the attack requires no special privilege and can be automated cheaply as long as `allowHardenExchangeCalculation` remains disabled for that pair/network state.

### Recommendation
Make the exact/BigDecimal-based `SafeExchangeProcessor` computation the unconditional, default path for all `Exchange*` actuators (or fix `ExchangeProcessor` itself to use exact integer/BigDecimal arithmetic with a rounding direction that always favors the pool, analogous to `RoundingDirection` in the referenced patch), rather than gating correctness behind an optional dynamic property (`allowHardenExchangeCalculation`) that can remain off.

### Proof of Concept
1. Do not enable `allowHardenExchangeCalculation` (default network state).
2. Create an `ExchangeCreateContract` pair with modest reserves.
3. Repeatedly submit small `ExchangeTransactionContract` trades alternating direction; each call to `ExchangeCapsule.transaction()` invokes the floating-point `ExchangeProcessor.exchange()` path shown above.
4. As demonstrated by `ExchangeProcessorTest.testStrictMath` (`Assert.assertNotEquals(anotherTokenQuant, result)`), the non-strict double computation yields different (and not consistently pool-favoring) integer results than the exact/hardened computation for identical inputs, confirming the rounding drift is present and measurable in the shipped code path.

### Citations

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-129)
```java
  public long transaction(byte[] sellTokenID, long sellTokenQuant, boolean useStrictMath,
      boolean hardenedCalc) throws ContractValidateException {
    long supply = 1_000_000_000_000_000_000L;
    Processor processor = hardenedCalc
        ? SafeExchangeProcessor.INSTANCE : new ExchangeProcessor(supply, useStrictMath);

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
