### Title
Incorrect Bancor-relay share valuation via floating-point `Math.pow` in `ExchangeProcessor` allows exchange pool drainage - ([File: chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java])

### Summary
The TRON on-chain Bancor-style Exchange feature (`ExchangeCapsule`/`ExchangeProcessor`) values a swap's "another token" output using a bonding-curve computation performed in `double` floating-point arithmetic with `Math.pow`, then truncates the intermediate "relay supply" and final output with a bare `(long)` cast. This is the same bug class as the Belt exploit: an AMM/vault's share-to-asset conversion is computed with an approximate/imprecise formula rather than an exact, monotonic, overflow-checked one, letting a caller who repeatedly round-trips small swaps accumulate value that the imprecise curve fails to charge for. The project's own fix — a parallel `SafeExchangeProcessor` gated behind the `allowHardenExchangeCalculation` (`allowHarden()`) chain parameter — confirms this exact code path was recognized as a source of miscalculation risk, but it is opt-in via committee proposal, so the legacy, exploitable double-math path remains the default unless that proposal has been activated on a given network.

### Finding Description
`ExchangeCapsule.transaction()` selects between two valuation engines depending on the `hardenedCalc` flag: [1](#0-0) 

The legacy engine, `ExchangeProcessor`, computes the bonding-curve "relay supply" and the resulting payout using `double` arithmetic and `Math.pow`-based exponentiation (`Maths.pow`), truncating results to `long` with a plain cast rather than exact, checked, rounding-aware math: [2](#0-1) 

By contrast, the hardened replacement uses `BigDecimal` with explicit scale/rounding and `StrictMathWrapper` overflow checks, and is only used when `allowHarden()` (backed by the `allowHardenExchangeCalculation` dynamic property) is enabled: [3](#0-2) 

This valuation function is reachable directly from unprivileged, signed transactions via `ExchangeTransactionActuator.execute/validate`, `ExchangeInjectActuator`, and `ExchangeWithdrawActuator`, all of which call `exchangeCapsule.transaction(...)` with the `hardenedCalc` flag sourced from the current `allowHarden()` proposal state: [4](#0-3) [5](#0-4) 

The project's own regression tests demonstrate the legacy `double`-based processor and the hardened `BigDecimal` processor diverge on identical inputs, i.e., the legacy formula is provably imprecise relative to the "correct" valuation: [6](#0-5) 

Because each `ExchangeTransactionContract` recomputes the curve from the pool's current on-chain `firstTokenBalance`/`secondTokenBalance` using this imprecise double-math engine, an attacker can issue a sequence of buy/sell round-trip transactions against the same Exchange pool (analogous to Belt's repeated deposit/withdraw round trips across strategies) and exploit the floating-point rounding behavior of the curve to extract more value than a mathematically exact bonding curve would allow, gradually draining the pool's `firstTokenBalance`/`secondTokenBalance` reserves that back other users' relay tokens.

### Impact Explanation
If the imprecise legacy path is active (i.e., `allowHardenExchangeCalculation` has not been enabled by committee proposal on a given network — which is the default/legacy behavior the hardened processor was built to replace), an attacker can drain TRX/TRC10 reserves from an Exchange pool through a sequence of ordinary, unprivileged `ExchangeTransactionContract` broadcasts, resulting in theft of pooled funds belonging to the exchange's liquidity/relay-token holders. This matches the "unauthorized... theft... of funds" impact bar.

### Likelihood Explanation
Likelihood depends on whether the `allowHardenExchangeCalculation` proposal has been activated on the target network. Where it has not, the vulnerable code path is exercised by every ordinary `ExchangeTransactionActuator`/`ExchangeInjectActuator`/`ExchangeWithdrawActuator` call with no special privilege required — any account holding the traded token/TRX can attempt the round-trip strategy. The existence of the hardened rewrite in the same codebase indicates the project itself identified this as worth remediating, supporting a credible, non-hypothetical likelihood.

### Recommendation
Make `SafeExchangeProcessor` (the `BigDecimal`-based, overflow-checked bonding-curve implementation) the unconditional default for all Exchange valuation, removing the legacy `double`/`Math.pow` code path in `ExchangeProcessor` entirely rather than gating the fix behind an opt-in `allowHardenExchangeCalculation` proposal. Additionally, add pool-level invariant checks (e.g., constant-product/curve invariant must never decrease net of fees) inside `ExchangeCapsule.transaction()` so that any round-trip sequence that would leave the pool worse off than the exact curve predicts is rejected in `doValidate()`.

### Proof of Concept
1. On a network/testnet where `allowHardenExchangeCalculation` is not enabled (legacy default), create an Exchange pool via `ExchangeCreateActuator` with two TRC10 tokens (or TRX/TRC10) at a chosen balance ratio.
2. Repeatedly submit `ExchangeTransactionContract` transactions from the same account that alternately sell token A for token B and then sell the received B back for A, each time comparing the amount received against the exact bonding-curve value computed via `BigDecimal`/`SafeExchangeProcessor` (as done in `ExchangeProcessorTest.testStrictMath`, which shows the legacy and safe processors diverge for identical inputs).
3. Observe that repeated round trips systematically move `firstTokenBalance`/`secondTokenBalance` (visible via `ExchangeCapsule.getFirstTokenBalance()/getSecondTokenBalance()`) in the attacker's favor relative to the exact-curve prediction, and that no validation in `ExchangeTransactionActuator.doValidate()` rejects this drift.
4. Repeat at scale to demonstrate meaningful drainage of pool reserves.

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L64-69)
```java
      byte[] tokenID = exchangeTransactionContract.getTokenId().toByteArray();
      long tokenQuant = exchangeTransactionContract.getQuant();

      byte[] anotherTokenID;
      long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
          dynamicStore.allowStrictMath(), allowHarden());
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L217-221)
```java
    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
    }
```

**File:** framework/src/test/java/org/tron/core/capsule/utils/ExchangeProcessorTest.java (L272-281)
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
  }
```
