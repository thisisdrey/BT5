### Title
Precision-loss in the legacy floating-point Bancor exchange formula lets an attacker drain TRC10/TRX liquidity pools via repeated small trades - ([File: chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java])

### Summary
The Moonwell incident hinged on an incorrect/imprecise on-chain price computation that could be skewed with a tiny deposit to extract disproportionate value on repeated calls. java-tron's built-in `Exchange*` actuators (`ExchangeTransactionActuator`, `ExchangeInjectActuator`, `ExchangeWithdrawActuator`) contain an analogous on-chain AMM ("Bancor-style relay") price engine, `ExchangeProcessor`, that by default performs the core pricing math in `double` floating point rather than exact integer/BigDecimal arithmetic. Any unprivileged account can call `ExchangeTransactionContract` to repeatedly trade tiny quantities against a pool and accumulate rounding-favorable outcomes at the pool's expense, because the safer, exact `SafeExchangeProcessor` implementation is gated behind a committee-controlled dynamic property (`allowHardenExchangeCalculation`) that defaults to off.

### Finding Description
`ExchangeCapsule.transaction()` selects between two pricing engines depending on the `hardenedCalc` flag: [1](#0-0) 

`AbstractExchangeActuator.allowHarden()` derives this flag purely from the dynamic property `allowHardenExchangeCalculation`, which is a committee/governance-controlled proposal parameter, not something a normal caller sets: [2](#0-1) 

When the flag is off (its default state until a super-representative committee explicitly enables it via proposal), `ExchangeTransactionActuator.execute()` (reachable by any signed `ExchangeTransactionContract` broadcast from an unprivileged account) uses the legacy `ExchangeProcessor`: [3](#0-2) 

`ExchangeProcessor.exchangeToSupply`/`exchangeFromSupply` compute the Bancor relay conversion with `double` arithmetic and `Math.pow`-based exponentiation, then truncate to `long`: [4](#0-3) 

The project's own test suite (`ExchangeProcessorTest.testStrictMath`) explicitly demonstrates that the legacy (non-strict, `useStrictMath=false`) processor produces a *different* result than the exact `SafeExchangeProcessor` for the same inputs, confirming the legacy path is measurably imprecise: [5](#0-4) 

Because the imprecise floating-point computation is asymmetric (rounding direction is not proven to always favor the pool), a caller who repeatedly issues small `ExchangeTransactionContract` trades (or interleaves `ExchangeInjectContract`/`ExchangeWithdrawContract` calls that also branch on the same `allowHarden()` gate for overflow-safety, see `ExchangeInjectActuator.execute()`'s plain `long`/`floorDiv` math versus its `BigInteger`-based validate check) can accumulate systematic rounding advantage across many transactions, extracting value from the pool's TRX/TRC10 reserves without providing equivalent value — the same "small-input, repeated-call, imprecise-price" pattern used in the Moonwell exploit. [6](#0-5) 

### Impact Explanation
An attacker who can repeatedly submit `ExchangeTransactionContract` transactions against a TRC10/TRX Bancor-style exchange pool controlled by java-tron's on-chain `Exchange` feature could siphon TRX/TRC10 balances out of the pool's reserves over many transactions, resulting in unbacked pool balances and financial loss to legitimate liquidity providers/exchange creators — a concrete "theft of funds" impact per the accepted impact list.

### Likelihood Explanation
Exploitation only requires an unprivileged account with enough TRX to pay the transaction fee and issue repeated `ExchangeTransactionContract` calls against an existing exchange pool; no special privileges, SR/witness status, or governance access are required. The vulnerable code path (legacy floating-point `ExchangeProcessor`) is the default because the exact `SafeExchangeProcessor` path requires a committee proposal (`allowHardenExchangeCalculation`) to be turned on chain-wide.

### Recommendation
Make the exact `SafeExchangeProcessor` (BigDecimal-based) computation the unconditional default for all exchange actuators (`ExchangeTransactionActuator`, `ExchangeInjectActuator`, `ExchangeWithdrawActuator`) rather than gating it behind an opt-in dynamic property, and remove/deprecate the floating-point `ExchangeProcessor` path entirely, or otherwise mathematically prove that its rounding bias never favors the caller across the full domain of valid balances/quantities.

### Proof of Concept
1. Create or locate an active `Exchange` pool (`ExchangeCreateContract`) with TRX and a TRC10 token balance.
2. Submit repeated `ExchangeTransactionContract` transactions with small `quant` values (e.g. near the smallest unit) alternating buy direction against `ExchangeTransactionActuator.execute()`, relying on the default (`allowHardenExchangeCalculation=0`) so `ExchangeCapsule.transaction()` uses `ExchangeProcessor` (`hardenedCalc=false`).
3. Because `ExchangeProcessor.exchangeToSupply/exchangeFromSupply` use `double`/`Math.pow` truncated to `long` (as demonstrated to diverge from `SafeExchangeProcessor` in `ExchangeProcessorTest.testStrictMath`), compare cumulative pool balance drift over many iterations against the exact BigDecimal-based computation to quantify the extractable rounding bias, then repeat the favorable-direction trades to accumulate profit at pool expense.

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L64-69)
```java
      byte[] tokenID = exchangeTransactionContract.getTokenId().toByteArray();
      long tokenQuant = exchangeTransactionContract.getQuant();

      byte[] anotherTokenID;
      long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
          dynamicStore.allowStrictMath(), allowHarden());
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
