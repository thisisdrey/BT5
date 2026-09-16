## Finding

### Title
TRC10 Exchange (Bancor-style) legacy calculation path can drain pool reserves via floating-point precision loss with no bounds check — ([File: chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java])

### Summary
The `MIMSpell2` exploit abused repeated tiny `borrow(1)`/`repay(1)` operations to accumulate rounding errors in an `elastic`/`base` debt-accounting formula until the protocol's internal state diverged from real backing, letting the attacker extract far more than deposited. The closest reachable analog in java-tron is the TRC10 `Exchange` (Bancor-curve) market, which is directly reachable by any unprivileged signed transaction (`ExchangeTransactionContract` via `ExchangeTransactionActuator`) and performs the equivalent bonding-curve conversion using unchecked `double` floating-point math with no invariant enforcement in its default (non-hardened) code path.

### Finding Description
`ExchangeCapsule.transaction()` computes the counter-token amount via a `Processor` and then updates the pool balances: [1](#0-0) 

The default `Processor` implementation, `ExchangeProcessor`, performs the Bancor relay conversion entirely with `double` arithmetic and `Math.pow`: [2](#0-1) 

Critically, the negative-balance invariant check in `ExchangeCapsule.transaction()` is only executed when `hardenedCalc` is `true`:

```
if (hardenedCalc && (newFirstTokenBalance < 0 || newSecondTokenBalance < 0)) {
  throw new ContractValidateException("Exchange balance must be >=0 after transaction");
}
```

`hardenedCalc` corresponds to the `allowHardenExchangeCalculation` dynamic parameter, which is a maintenance/proposal-gated flag (default off) rather than an always-on invariant: [3](#0-2) 

When the hardened flag is not enabled — the historical/default behavior for this feature — `ExchangeTransactionActuator.execute()` blindly commits whatever `buyTokenQuant`/pool balances the double-precision `ExchangeProcessor` produces, with no post-hoc sanity check that the counter-token balance stays non-negative or that value is conserved: [4](#0-3) 

The project's own test suite demonstrates that the legacy `double`-based math and the newer `BigDecimal`-based `SafeExchangeProcessor` diverge for identical inputs: [5](#0-4) 

This is structurally the same bug class as MIMSpell2: a bonding/accounting formula computed with imprecise arithmetic, invoked repeatedly by an unprivileged caller (`ExchangeTransactionActuator`, reachable from any signed transaction), whose result is trusted and persisted without an end-to-end invariant check in the default code path.

### Impact Explanation
If an attacker finds token-balance/quant combinations where the double-precision `Math.pow` computation in `ExchangeProcessor.exchangeFromSupply`/`exchangeToSupply` systematically rounds in the attacker's favor, repeated small `ExchangeTransactionContract` calls (analogous to the repeated `borrow(1)/repay(1)` loop in the report) could extract counter-token value disproportionate to what was deposited, potentially driving one side of the pool's balance toward zero or below what genuine trading would allow — an unbacked-balance / theft-of-funds outcome, exactly the category explicitly listed as in-scope ("exchange and market order handling").

### Likelihood Explanation
Reaching this code path only requires broadcasting ordinary `ExchangeCreateContract`/`ExchangeTransactionContract` transactions against a TRC10 exchange pool — fully within reach of an unprivileged transaction broadcaster. No special privileges, malicious SR/witness, or off-chain component are needed. The likelihood of finding an exploitable rounding sequence depends on the specific `Math.pow` rounding behavior for particular balance ratios, which would require dedicated numerical analysis/fuzzing to pin down concrete inputs — the report itself does not provide java-tron-specific numbers, only the general bug-class hint.

### Recommendation
- Enforce the non-negative/no-overshoot invariant check (`newFirstTokenBalance < 0 || newSecondTokenBalance < 0`, and ideally `buyTokenQuant <= buyTokenBalance`) unconditionally in `ExchangeCapsule.transaction()`, not only when `hardenedCalc` is true.
- Make the `SafeExchangeProcessor` (BigDecimal-based) computation the default/only path for `ExchangeTransactionActuator` and `ExchangeWithdrawActuator`, retiring the unchecked `double`-based `ExchangeProcessor`.
- Add fuzz/property tests asserting that for any sequence of trades, `firstTokenBalance * secondTokenBalance` (or an equivalent conservation invariant) never decreases in the attacker's favor beyond fee/rounding tolerance.

### Proof of Concept
No concrete java-tron-specific input triplet was identified within the scope of this analysis that reliably reproduces an exploitable overshoot; the finding is based on structural analysis showing the invariant check is conditionally skipped in the default (non-hardened) path — a background engineering session with numeric fuzzing of `ExchangeProcessor.exchange` inputs (large `sellTokenBalance`/`buyTokenBalance`, small `sellTokenQuant`, repeated calls) against the `hardenedCalc=false` branch would be needed to construct an exact repeatable transaction sequence.

### Citations

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-168)
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

    } else {
      buyTokenQuant = processor.exchange(secondTokenBalance,
          firstTokenBalance,
          sellTokenQuant);
      newFirstTokenBalance = hardenedCalc
          ? StrictMathWrapper.subtractExact(firstTokenBalance, buyTokenQuant)
          : firstTokenBalance - buyTokenQuant;
      newSecondTokenBalance = hardenedCalc
          ? StrictMathWrapper.addExact(secondTokenBalance, sellTokenQuant)
          : secondTokenBalance + sellTokenQuant;

    }

    if (hardenedCalc && (newFirstTokenBalance < 0 || newSecondTokenBalance < 0)) {
      throw new ContractValidateException("Exchange balance must be >=0 after transaction");
    }
    this.exchange = this.exchange.toBuilder()
        .setFirstTokenBalance(newFirstTokenBalance)
        .setSecondTokenBalance(newSecondTokenBalance)
        .build();

    return buyTokenQuant;
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L61-96)
```java
      byte[] firstTokenID = exchangeCapsule.getFirstTokenId();
      byte[] secondTokenID = exchangeCapsule.getSecondTokenId();

      byte[] tokenID = exchangeTransactionContract.getTokenId().toByteArray();
      long tokenQuant = exchangeTransactionContract.getQuant();

      byte[] anotherTokenID;
      long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
          dynamicStore.allowStrictMath(), allowHarden());

      if (Arrays.equals(tokenID, firstTokenID)) {
        anotherTokenID = secondTokenID;
      } else {
        anotherTokenID = firstTokenID;
      }

      long newBalance = subtractExact(accountCapsule.getBalance(), calcFee());
      accountCapsule.setBalance(newBalance);

      if (Arrays.equals(tokenID, TRX_SYMBOL_BYTES)) {
        accountCapsule.setBalance(subtractExact(newBalance, tokenQuant));
      } else {
        accountCapsule.reduceAssetAmountV2(tokenID, tokenQuant, dynamicStore, assetIssueStore);
      }

      if (Arrays.equals(anotherTokenID, TRX_SYMBOL_BYTES)) {
        accountCapsule.setBalance(addExact(newBalance, anotherTokenQuant));
      } else {
        accountCapsule
            .addAssetAmountV2(anotherTokenID, anotherTokenQuant, dynamicStore, assetIssueStore);
      }

      accountStore.put(accountCapsule.createDbKey(), accountCapsule);

      Commons.putExchangeCapsule(exchangeCapsule, dynamicStore, exchangeStore, exchangeV2Store,
          assetIssueStore);
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
