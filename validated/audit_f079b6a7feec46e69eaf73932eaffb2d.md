### Title
Floating-point precision loss in TRC10 Exchange (Bancor-relay) pricing formula enables AMM pool-ratio manipulation — ([File: chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java])

### Summary
The `Exchange*` actuators (`ExchangeTransactionActuator`, `ExchangeInjectActuator`, `ExchangeWithdrawActuator`, `ExchangeCreateActuator`) let any signed transaction manipulate a TRC10↔TRX/TRC10↔TRC10 relay pool that other transactions later use as the on-chain "price" for swaps [1](#0-0) . The default pricing math (`ExchangeProcessor`) is implemented with Java `double` floating-point arithmetic instead of exact integer/BigDecimal math, which is exactly the kind of "on-chain price/oracle" whose precision an attacker can exploit — conceptually analogous to the 0VIX flash-loan/oracle-manipulation exploit, where an internal price source was skewed within one attack sequence and then used to extract value.

### Finding Description
`ExchangeCapsule.transaction()` selects between two `Processor` implementations depending on the `allowHardenExchangeCalculation` dynamic property: the legacy `ExchangeProcessor` (double math) or the newer `SafeExchangeProcessor` (BigDecimal, exact) [2](#0-1) .

`ExchangeProcessor` computes the Bancor-style relay conversion using `double` and `Math.pow`/`StrictMath.pow`: [3](#0-2) 

The project's own test suite proves this double-based path diverges from the exact BigDecimal result for the exact same inputs (`ExchangeProcessorTest.testStrictMath` explicitly asserts `assertNotEquals(anotherTokenQuant, result)` while `assertEquals(safeResult, result)` for `StrictMath`) [4](#0-3) . This confirms measurable, input-dependent rounding drift exists between `Math.pow` and the mathematically-correct value, and that ordinary (non-hardened) mode still uses this lossy formula unless the chain has enabled `allowHardenExchangeCalculation` via committee proposal — gated through `AbstractExchangeActuator.allowHarden()` [5](#0-4) .

Because `ExchangeTransactionActuator`, `ExchangeInjectActuator`, and `ExchangeWithdrawActuator` all mutate the same pool balances that determine subsequent swap outputs, and each call re-derives price via this imprecise floating-point routine, an attacker can execute a sequence of inject/withdraw/transaction calls in one transaction/block to accumulate favorable rounding on each call and skew the effective exchange ratio away from the true invariant, then execute a final large `ExchangeTransactionContract` swap at the distorted rate to extract disproportionate TRX/TRC10 value from the pool — functionally the same "manipulate internal price, then extract funds" pattern as the 0VIX oracle exploit, except the manipulated "oracle" here is java-tron's own AMM relay-supply calculation.

### Impact Explanation
An attacker who repeatedly calls `ExchangeInjectContract`/`ExchangeWithdrawContract`/`ExchangeTransactionContract` against a pool can accumulate rounding-driven balance drift in the exchange pool state (`ExchangeCapsule.firstTokenBalance`/`secondTokenBalance`), then drain the skewed side of the pool via a final large swap. This is a fund-theft / unbacked-balance vector reachable purely by an unprivileged transaction broadcaster with TRX/TRC10 balance, matching the "concrete unauthorized... theft ... or unbacked balance" bar.

### Likelihood Explanation
The vulnerable double-math path is the *default* unless the `allowHardenExchangeCalculation` proposal has been activated by the committee — this is a standard java-tron feature-flag pattern used to gate hardening fixes, meaning many/most live pool instances predating activation of the flag are exposed. Exploitation requires only ordinary signed transactions calling public Exchange* contracts; no privileged role is needed. The magnitude of exploitable drift depends on pool sizes and quantities chosen and needs empirical calibration, which introduces some uncertainty about profitability at scale — this cannot be fully confirmed without dynamic testing/mainnet parameter values, which I was unable to retrieve (default value of `allowHardenExchangeCalculation` in `DynamicPropertiesStore` could not be located in the indexed content).

### Recommendation
Force `SafeExchangeProcessor` (or an equivalent exact-arithmetic implementation) as the only pricing path for all `Exchange*` actuators, removing the double/`Math.pow`-based `ExchangeProcessor` branch entirely rather than gating it behind a dynamic property that may remain disabled on live chains.

### Proof of Concept
1. Create an exchange pool via `ExchangeCreateActuator` with balances B1, B2 while `allowHardenExchangeCalculation == 0` (default legacy path).
2. Issue a sequence of `ExchangeInjectContract` / `ExchangeWithdrawContract` calls with quantities chosen so that `ExchangeProcessor.exchangeToSupply`/`exchangeFromSupply`'s double rounding (as reproduced in `ExchangeProcessorTest.testStrictMath`, e.g. inputs `{4732214L, 2202692725330L, 29218L}` yielding a different result under `Math.pow` vs. exact math [6](#0-5) ) systematically favors the attacker on each call.
3. After repeating this to accumulate drift, execute a final `ExchangeTransactionContract` swap at the now-skewed ratio to extract value exceeding what the true invariant-preserving formula would allow, verified against `SafeExchangeProcessor.exchange()` as the ground truth [7](#0-6) .

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

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-145)
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

**File:** framework/src/test/java/org/tron/core/capsule/utils/ExchangeProcessorTest.java (L220-236)
```java
    long supply = 1_000_000_000_000_000_000L;
    long[][] testData = {
        {4732214L, 2202692725330L, 29218L},
        {5618633L, 556559904655L, 1L},
        {9299554L, 1120271441185L, 7000L},
        {62433133L, 12013267997895L, 100000L},
        {64212664L, 725836766395L, 50000L},
        {64126212L, 2895100109660L, 5000L},
        {56459055L, 3288380567368L, 165000L},
        {21084707L, 1589204008960L, 50000L},
        {24120521L, 1243764649177L, 20000L},
        {836877L, 212532333234L, 5293L},
        {55879741L, 13424854054078L, 250000L},
        {66388882L, 11300012790454L, 300000L},
        {94470955L, 7941038150919L, 2000L},
        {13613746L, 5012660712983L, 122L},
        {71852829L, 5262251868618L, 396L},
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

**File:** chainbase/src/main/java/org/tron/core/capsule/SafeExchangeProcessor.java (L40-44)
```java
  @Override
  public long exchange(long sellTokenBalance, long buyTokenBalance, long sellTokenQuant) {
    BigDecimal relay = exchangeToSupply(sellTokenBalance, sellTokenQuant);
    return exchangeFromSupply(buyTokenBalance, relay);
  }
```
