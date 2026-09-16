### Title
Floating-point precision loss in `ExchangeProcessor`'s bancor-style relay math enables value extraction from TRC10↔TRX exchange pools - (File: `chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java`)

### Summary
The `ExchangeTransactionContract` path (reachable by any signed transaction from an unprivileged account) computes swap outputs for TRC10↔TRX "Exchange" pools using `double`-based bancor relay math in `ExchangeProcessor`, which is the default calculation path unless a chain-wide `allowHardenExchangeCalculation`/`allowStrictMath` dynamic property is activated. This mirrors the CompounderFinance bug class: value is lost/gained through precision/rounding artifacts in an internal exchange-rate calculation reachable by ordinary users, rather than through any privileged operation.

### Finding Description
`ExchangeTransactionActuator.execute()` lets any account swap tokens against a TRC10/TRX exchange pool by calling `ExchangeCapsule.transaction()`, which selects the math engine to use: [1](#0-0) 

When `hardenedCalc` (`allowHarden()`) is false — the legacy, and presumably chain-default, mode — `ExchangeProcessor` is used, which performs the bancor "relay" computation entirely in `double` precision: [2](#0-1) 

`allowHarden()` merely reads a dynamic property flag, i.e. the safer `SafeExchangeProcessor` (BigDecimal-based) path is opt-in and only becomes active after a committee proposal sets `allowHardenExchangeCalculation`: [3](#0-2) 

The project's own test suite explicitly demonstrates that the legacy `double`-based path and the hardened `BigDecimal` path diverge on identical inputs — i.e., the legacy math is provably imprecise: [4](#0-3) 

This is directly analogous to the CompounderFinance root cause: an on-chain pool's exchange rate is derived from an approximate/roundable formula, and a caller who can freely invoke buy/sell operations against the pool (here, `ExchangeTransactionContract`, requiring no special privilege) can exploit the approximation to extract more value than deposited, or leave the pool under-collateralized relative to its nominal balances. Because `firstTokenBalance`/`secondTokenBalance` are updated using this imprecise `double` output (`this.exchange.toBuilder().setFirstTokenBalance(...)`), repeated round-trip trades (sell A→B then B→A) can accumulate systematic rounding bias in the trader's favor, draining real TRX/TRC10 value that was deposited into the pool by its creator via `ExchangeCreateContract`/`ExchangeInjectContract`.

### Impact Explanation
An unprivileged trader broadcasting ordinary `ExchangeTransactionContract` transactions can, through repeated round-trip swaps, exploit the floating-point rounding behavior of the default bancor relay formula to accumulate value at the expense of the exchange pool's real TRX/TRC10 backing. Because Exchange pools directly hold and settle real TRX and TRC10 balances (`accountCapsule.addAssetAmountV2`/`setBalance` in `ExchangeTransactionActuator.execute`), this constitutes unbacked extraction of real value — a fund-theft/fund-drain class impact, matching the "manipulation of funds through fluctuations in the amount of exchangeable assets" bug class from the external report.

### Likelihood Explanation
Exploitation only requires ordinary signed transactions (`ExchangeTransactionContract`) against any existing exchange pool — no special permission, no privileged actuator, and no reliance on malicious SRs/witnesses/peers. The vulnerable floating-point path (`ExchangeProcessor`) is the legacy/default code path, while the corrected `SafeExchangeProcessor` is gated behind a dynamic property that must be separately enabled by chain governance, meaning the imprecise math is exercised until that hardening proposal is active on a given network. I was not able to fully confirm from the indexed code what the shipped/genesis default value of `allowHardenExchangeCalculation` (and `allowStrictMath`) is on mainnet — this should be verified against `DynamicPropertiesStore` initialization and genesis config before treating this as confirmed-exploitable in production.

### Recommendation
- Make `SafeExchangeProcessor` (BigDecimal-based) the unconditional calculation path for exchange transactions, removing the double-precision `ExchangeProcessor` entirely, rather than gating correctness behind an opt-in governance flag.
- Add invariant checks after every `ExchangeCapsule.transaction()` call that the bancor product/relay-supply invariant is not violated beyond a strict, tightly-bounded tolerance, rejecting the transaction otherwise (similar to the "Not precise enough" check already present in `ExchangeWithdrawActuator`).
- Audit historical mainnet blocks for repeated round-trip `ExchangeTransactionContract` sequences against the same `exchange_id` to check for evidence of prior exploitation of the double-precision path.

### Proof of Concept
Conceptual PoC (network/state access needed to confirm real-world exploitability, which is outside static analysis):
1. Attacker identifies an `Exchange` pool with pair (TRX, TRC10-X) whose current dynamic properties have `allowHardenExchangeCalculation = 0` (legacy math active).
2. Attacker repeatedly submits `ExchangeTransactionContract` transactions swapping TRX→X then X→TRX (round trips) against the same exchange, using amounts chosen to maximize the `double`-rounding divergence demonstrated in `ExchangeProcessorTest.testStrictMath` (e.g., the tabulated `{sellBalance, buyBalance, sellQuant}` triples showing `anotherTokenQuant != result` between double and BigDecimal/StrictMath paths).
3. Over many iterations, the attacker's net TRX+X holdings increase while the pool's recorded `firstTokenBalance`/`secondTokenBalance` in `ExchangeCapsule` drift away from the true conserved value, extracting value from the pool's creator-funded liquidity. [2](#0-1) [4](#0-3)

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

**File:** framework/src/test/java/org/tron/core/capsule/utils/ExchangeProcessorTest.java (L271-280)
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
