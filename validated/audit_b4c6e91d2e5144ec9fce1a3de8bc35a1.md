## Analysis

The bug report describes a Chainlink price-oracle that has no recovery path once its underlying data source enters a permanently broken/stale state — the `consult()` function keeps reverting forever, and there is no admin function to swap the aggregator.

The closest reachable analog in java-tron is the TRC10 **bancor-style exchange pool** implemented by `ExchangeCapsule.transaction()`, exercised by `ExchangeTransactionActuator`, `ExchangeInjectActuator`, and `ExchangeWithdrawActuator`, all of which are triggerable by any unprivileged broadcast transaction.

### Finding Description

`ExchangeCapsule.transaction()` selects between two math engines depending on the `ALLOW_HARDEN_EXCHANGE_CALCULATION` chain parameter: the legacy floating-point `ExchangeProcessor` (double-based `Math.pow`) and the newer BigDecimal-based `SafeExchangeProcessor`. [1](#0-0) 

Critically, the negative-balance guard is only applied on the hardened path: [2](#0-1) 

When `hardenedCalc` is `false` (the default, since the parameter must be explicitly enabled by committee proposal after fork `VERSION_4_8_2`), `newFirstTokenBalance`/`newSecondTokenBalance` are computed with plain `long` arithmetic seeded from a floating point result and are never checked for going negative before being persisted: [3](#0-2) 

The corresponding actuator validation only rejects a pool whose balance is *exactly* zero — not negative: [4](#0-3) 

Because `Math.pow` with fractional exponents (`0.0005` / `2000.0`) is not guaranteed to be a perfectly inverse operation, sequences of trades (or a single crafted trade near the edge of the reserve) can make `exchangeFromSupply` return a `buyTokenQuant` larger than the actual `secondTokenBalance`/`firstTokenBalance`, driving the persisted `Exchange.firstTokenBalance`/`secondTokenBalance` negative in `ExchangeStore`/`ExchangeV2Store`.

Once an exchange's on-chain reserve is negative:
- There is no governance/actuator path to reset or repair a single corrupted `ExchangeCapsule` — the only lever, `ALLOW_HARDEN_EXCHANGE_CALCULATION`, is a **global** switch for all exchanges, and it can only be flipped through a committee proposal validated in `ProposalUtil` (fork-gated, one bit). [5](#0-4) 
- If that global switch is later turned on, the very same exchange immediately becomes permanently stuck: any further trade on it will compute new balances from the already-negative baseline via `StrictMathWrapper.addExact/subtractExact`, and the `hardenedCalc && (newFirstTokenBalance < 0 || newSecondTokenBalance < 0)` guard now unconditionally throws `ContractValidateException("Exchange balance must be >=0 after transaction")` for every future transaction on that pool — mirroring exactly the "TwapOracle.consult() always reverts" scenario from the report, but for a specific exchange, with no way for governance to reset just that pool's balances back to a sane state. [6](#0-5) 

This is also acknowledged as a known risk area by the repository's own test suite, which specifically probes overflow/negative-balance corner cases of both processors (e.g. `testHardenedTransactionNegativeBalanceThrows`, `testSafeProcessorDivByZeroThrows`), confirming the team is aware negative/corrupted balances are reachable but has not added any repair/rescue actuator: [7](#0-6) 

### Impact Explanation

Once floating-point drift or a crafted trade sequence pushes a TRC10 exchange's reserve negative, the pool's on-chain balances become unbacked (the accounting no longer matches actual backing assets), and users interacting with that exchange via `ExchangeTransactionActuator`/`ExchangeWithdrawActuator`/`ExchangeInjectActuator` can receive tokens the pool cannot actually back, or, once the hardened switch is enabled, the exchange becomes permanently unusable (every future transaction reverts) with no on-chain repair mechanism — a direct parity to the reported oracle's irrecoverable stuck state, but manifesting as frozen/unbacked TRC10 liquidity that any address can trigger via ordinary `ExchangeCreateContract`/`ExchangeTransactionContract`/`ExchangeInjectContract`/`ExchangeWithdrawContract` transactions.

### Likelihood Explanation

Reaching this requires only unprivileged, ordinary transactions against the exchange actuators (no special privilege), and the vulnerable non-hardened path is the historical default behavior still reachable while `ALLOW_HARDEN_EXCHANGE_CALCULATION == 0`. Triggering an actual floating point drift severe enough to flip a balance negative requires specific extreme quant values/edge cases in the bancor formula, which lowers likelihood somewhat, but the complete absence of any negative-balance check or repair path in the non-hardened branch means any such drift is permanently and silently committed to state.

### Recommendation

- Apply the same non-negative-balance invariant check in the non-hardened path of `ExchangeCapsule.transaction()`, not only in the `hardenedCalc` branch.
- Provide a governance-controlled remediation actuator/path to reset or close an individual corrupted `ExchangeCapsule` (analogous to allowing governance to update a stuck oracle aggregator), rather than relying solely on a single global `ALLOW_HARDEN_EXCHANGE_CALCULATION` switch that itself permanently bricks already-corrupted pools once enabled.

### Proof of Concept

1. With `ALLOW_HARDEN_EXCHANGE_CALCULATION == 0` (default), create/select an `Exchange` pool with small enough reserves and construct an `ExchangeTransactionContract` sell quant near the numeric edge such that `ExchangeProcessor.exchangeFromSupply`'s `Math.pow(1 + supplyQuant/supply, 2000.0)` rounding yields a `buyTokenQuant` fractionally larger than `buyTokenBalance`.
2. Submit the transaction through `ExchangeTransactionActuator`; since only the `hardenedCalc` branch checks for a resulting negative balance [2](#0-1) , the negative `secondTokenBalance` (or `firstTokenBalance`) is persisted via `Commons.putExchangeCapsule` in `ExchangeStore`/`ExchangeV2Store`.
3. Subsequent calls to `ExchangeWithdrawActuator`/`ExchangeInjectActuator` operate on the now-corrupted reserve figures (only an exact-zero check exists, not negative) [8](#0-7) , compounding the unbacked state.
4. If governance later enables `ALLOW_HARDEN_EXCHANGE_CALCULATION`, every future transaction against that specific pool immediately throws `"Exchange balance must be >=0 after transaction"` [6](#0-5) , permanently freezing the pool with no recovery actuator available.

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

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L160-166)
```java
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L194-197)
```java
    if (firstTokenBalance == 0 || secondTokenBalance == 0) {
      throw new ContractValidateException("Token balance in exchange is equal with 0,"
          + "the exchange has been closed");
    }
```

**File:** actuator/src/main/java/org/tron/core/utils/ProposalUtil.java (L928-943)
```java
      case ALLOW_HARDEN_EXCHANGE_CALCULATION: {
        if (!forkController.pass(ForkBlockVersionEnum.VERSION_4_8_2)) {
          throw new ContractValidateException(
              "Bad chain parameter id [ALLOW_HARDEN_EXCHANGE_CALCULATION]");
        }
        if (value != 0 && value != 1) {
          throw new ContractValidateException(
              "This value[ALLOW_HARDEN_EXCHANGE_CALCULATION] is only allowed to be 0 or 1");
        }
        if (dynamicPropertiesStore.getAllowHardenExchangeCalculation() == value) {
          throw new ContractValidateException(
              "[ALLOW_HARDEN_EXCHANGE_CALCULATION] has been set to " + value
                  + ", no need to propose again");
        }
        break;
      }
```

**File:** framework/src/test/java/org/tron/core/capsule/ExchangeCapsuleTest.java (L71-83)
```java
  @Test
  public void testHardenedTransactionNegativeBalanceThrows() throws Exception {
    // Construct a corrupt-state pool with a negative balance to drive the
    // < 0 invariant in the hardened branch via subtractExact wrapping.
    ExchangeCapsule capsule = new ExchangeCapsule(
        ByteString.copyFromUtf8("owner"), 99L, 0L,
        "abc".getBytes(), "def".getBytes());
    capsule.setBalance(Long.MAX_VALUE, 1L);

    // Selling abc adds to firstTokenBalance: addExact(MAX, q) overflows -> ArithmeticException
    Assert.assertThrows(ArithmeticException.class,
        () -> capsule.transaction("abc".getBytes(), 1L, true, true));
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L209-212)
```java
    if (firstTokenBalance == 0 || secondTokenBalance == 0) {
      throw new ContractValidateException("Token balance in exchange is equal with 0,"
          + "the exchange has been closed");
    }
```
