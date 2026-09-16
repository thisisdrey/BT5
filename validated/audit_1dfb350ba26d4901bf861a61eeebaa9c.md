### Title
Floating-point Bancor-formula rounding in the on-chain TRX/TRC10 Exchange lets a trader extract value from the pool via price manipulation - (File: chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java)

### Summary
The BH Token incident is a classic AMM "price manipulation" attack where an attacker exploited the pool's price/reserve math to extract funds disproportionate to the value deposited. The closest reachable analog in java-tron is the built-in decentralized `Exchange` feature (TRX↔TRC10 or TRC10↔TRC10 AMM pools), whose default (non-hardened) swap math is computed with IEEE-754 `double` arithmetic in `ExchangeProcessor` rather than exact integer/BigDecimal math.

### Finding Description
`ExchangeTransactionActuator.execute()` calls `exchangeCapsule.transaction(tokenID, tokenQuant, dynamicStore.allowStrictMath(), allowHarden())` [1](#0-0) , which selects between the legacy `ExchangeProcessor` (double-based, Bancor-style formula) and the newer `SafeExchangeProcessor` (BigDecimal-based) depending on `allowHarden()`/`allowHardenExchangeCalculation()` [2](#0-1) .

The default/legacy path, `ExchangeProcessor.exchangeToSupply` / `exchangeFromSupply`, performs the reserve-ratio calculation using `double` and `Math.pow`, and truncates results via a cast to `long`: [3](#0-2) 

This floating-point pricing curve is inherently imprecise for very small or very large `sellTokenQuant`/reserve ratios, and truncation via `(long) issuedSupply` / `(long) exchangeBalance` discards fractional remainder in a way that is not guaranteed to always round in the pool's favor. Because `ExchangeTransactionActuator` only enforces a user-supplied minimum-output check (`tokenExpected`) in `doValidate()` [4](#0-3)  and applies no floor/cap on how much the reserve ratio can move in a single transaction, an unprivileged transaction broadcaster can repeatedly submit `ExchangeTransactionContract` calls that walk the reserve balances into regions where the double-precision `pow()` rounding consistently favors the trader over the pool, incrementally draining `firstTokenBalance`/`secondTokenBalance` while each individual trade satisfies its own `tokenExpected` slippage check.

### Impact Explanation
If the rounding bias is consistently favorable to the caller (as floating-point truncation of a monotonic curve can be, depending on reserve magnitudes), a party controlling a series of ordinary account transactions can drain TRX/TRC10 reserves from an Exchange pool beyond what the invariant math intends — a concrete unauthorized transfer of pooled assets, matching the "unbacked balance / theft of funds" impact bar for this scan.

### Likelihood Explanation
This is reachable purely via a signed `ExchangeTransactionContract` transaction from any account with a small pool position size — no special privileges, precompiles, or contract deployment required, and it is unauthenticated beyond a normal signed transaction. However, actual exploitability (magnitude of drift, and whether the network already runs with `allowHardenExchangeCalculation` enabled) depends on chain configuration; I could not confirm the current default value of `allowHardenExchangeCalculation` (the proposal-controlled flag) in `DynamicPropertiesStore`, since I was unable to locate its getter/default constant within the available index. If the hardened (BigDecimal, `SafeExchangeProcessor`) path is already active by default in this snapshot, this analog is materially weaker (mitigated) — this is the main uncertainty in this finding.

### Recommendation
- Confirm/verify the on-chain default and current value of `allowHardenExchangeCalculation`; if disabled, activate the hardened `SafeExchangeProcessor` path network-wide via governance proposal.
- Independently of the flag, add invariant-preserving checks in `ExchangeCapsule.transaction()` such as verifying that `firstTokenBalance * secondTokenBalance` (or the appropriate bonding-curve invariant) does not decrease after a trade, which would catch any residual rounding-direction bugs regardless of which processor is used.
- Add fuzz/differential tests comparing `ExchangeProcessor` (double) output against `SafeExchangeProcessor` (BigDecimal) output across a wide range of reserve ratios and quantities to bound the maximum discrepancy, and reject transactions whose discrepancy exceeds a safe tolerance.

### Proof of Concept
Not independently executable from the index alone — this requires running a testnet/simulation with `allowHardenExchangeCalculation` disabled (legacy default), creating an `ExchangeCreateContract` pool, then issuing a sequence of `ExchangeTransactionContract` trades with quantities chosen at the edges of the reserve ratio (very small `sellTokenQuant` relative to `newBalance`, per `exchangeToSupply`) and comparing cumulative reserve changes against the invariant, similar to the existing test harness in `framework/src/test/java/org/tron/core/actuator/ExchangeTransactionActuatorTest.java` and `framework/src/test/java/org/tron/core/capsule/ExchangeCapsuleTest.java` [5](#0-4) , but iterated at scale to detect a statistically consistent rounding bias rather than a single-trade discrepancy.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L67-69)
```java
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

**File:** framework/src/test/java/org/tron/core/capsule/ExchangeCapsuleTest.java (L108-140)
```java
  @Test
  public void testExchange() throws ContractValidateException {
    long sellBalance = 100000000L;
    long buyBalance = 100000000L;

    byte[] key = ByteArray.fromLong(1);

    ExchangeCapsule exchangeCapsule;
    try {
      exchangeCapsule = chainBaseManager.getExchangeStore().get(key);
      exchangeCapsule.setBalance(sellBalance, buyBalance);

      long sellQuant = 1_000_000L;
      byte[] sellID = "abc".getBytes();
      boolean useStrictMath = chainBaseManager.getDynamicPropertiesStore().allowStrictMath();
      long result = exchangeCapsule.transaction(sellID, sellQuant, useStrictMath);
      Assert.assertEquals(990_099L, result);
      sellBalance += sellQuant;
      Assert.assertEquals(sellBalance, exchangeCapsule.getFirstTokenBalance());
      buyBalance -= result;
      Assert.assertEquals(buyBalance, exchangeCapsule.getSecondTokenBalance());

      sellQuant = 9_000_000L;
      long result2 = exchangeCapsule.transaction(sellID, sellQuant, true, true);
      Assert.assertEquals(9090909L, result + result2);
      sellBalance += sellQuant;
      Assert.assertEquals(sellBalance, exchangeCapsule.getFirstTokenBalance());
      buyBalance -= result2;
      Assert.assertEquals(buyBalance, exchangeCapsule.getSecondTokenBalance());

    } catch (ItemNotFoundException e) {
      Assert.fail();
    }
```
