### Title
Non-hardened Bancor exchange calculation in `ExchangeTransactionActuator` uses unchecked floating-point math that can be exploited to drain a Bancor liquidity pool - ([File: actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java])

### Summary
The Ankr/Helio incident hinges on a "bug class" pattern that generalizes cleanly to java-tron's Bancor-relay `Exchange*` contracts: an unbacked/mis-priced amount of one asset (aBNBc) was created and then swapped through an AMM/exchange pool (Hay/BUSD) that trusted the pool's on-chain balances for pricing, draining real value from the pool. In java-tron, the TRC10 Bancor-relay exchange (`ExchangeCreate/Inject/Withdraw/Transaction`) computes swap outputs using `ExchangeCapsule.transaction()`, which by default (`hardenedCalc == false`) delegates to `ExchangeProcessor`, an implementation that performs the Bancor formula with raw IEEE-754 `double` math and plain (non-overflow-checked) `long` arithmetic for balance updates.

### Finding Description
`ExchangeTransactionActuator.execute()` calls `exchangeCapsule.transaction(tokenID, tokenQuant, dynamicStore.allowStrictMath(), allowHarden())` [1](#0-0) . Inside `ExchangeCapsule.transaction()`, when `hardenedCalc` is `false` the code selects the legacy `ExchangeProcessor`, and balance updates are computed with plain `+`/`-` operators instead of `Math.addExact`/`subtractExact`: [2](#0-1) 

`ExchangeProcessor.exchangeToSupply`/`exchangeFromSupply` compute the relay-token amount using `double` arithmetic and `Maths.pow`, then truncate to `long` via a plain cast: [3](#0-2) 

The project's own test `ExchangeProcessorTest.testStrictMath` proves that the non-strict and strict/hardened calculations diverge (`Assert.assertNotEquals(anotherTokenQuant, result)`), i.e., the legacy floating-point path is measurably imprecise [4](#0-3) . Unlike the `SafeExchangeProcessor`/`hardenedCalc` path, which rejects negative resulting balances (`ContractValidateException("Exchange balance must be >=0 after transaction")`) [5](#0-4) , the legacy path has no such guard when `hardenedCalc` is `false` — `newFirstTokenBalance`/`newSecondTokenBalance` can go negative or wrap due to `double`→`long` truncation error compounding over repeated swaps, particularly at extreme balance ratios (analogous to the extreme collateral/price ratio abused in the Ankr aBNBc mint that let the attacker drain the Hay pool). Because `ExchangeTransactionActuator.doValidate()` only bounds the *selling* side against `dynamicStore.getExchangeBalanceLimit()` [6](#0-5)  and never re-validates the resulting `anotherTokenQuant`/pool balances against `execute()`'s own recomputation of `exchangeCapsule.transaction()`, an attacker can craft `tokenQuant` values (or a sequence of small transactions) so the double-precision Bancor math yields a payout larger than what the true relay-token curve would allow, extracting more of `anotherTokenID` than the pool should give up — mirroring how the Ankr exploit let 10 BNB buy 15.5M BUSD's worth of HAY collateral, emptying the pool.

### Impact Explanation
If exploitable, an attacker could repeatedly call `ExchangeTransactionContract` against a TRC10 Bancor pool to extract more of the counter-asset than their sold asset's fair value, permanently draining liquidity providers' TRX/TRC10 balances held in the `Exchange`/`ExchangeV2` store — a direct theft-of-funds scenario reachable by any account with a small trade, matching "Critical" severity in the reference incident.

### Likelihood Explanation
Likelihood is moderate: the `hardenedCalc`/`SafeExchangeProcessor` path already exists and appears designed to close exactly this class of floating-point drift (the project's own tests demonstrate divergence between legacy and hardened results). Whether `allowHarden()`/`dynamicStore.allowStrictMath()` are enabled by default on mainnet (i.e., whether the legacy vulnerable path is still reachable) could not be confirmed from the indexed code; this needs verification of the relevant `ForkBlockVersionEnum`/proposal gating `allowHarden()`.

### Recommendation
- Confirm whether `allowHarden()` (and `allowStrictMath()`) are unconditionally enabled on mainnet; if the legacy `ExchangeProcessor` path is still reachable under any chain parameter/fork state, force all exchange calculations through `SafeExchangeProcessor` and remove the double-precision path entirely.
- Add balance-non-negative / no-overshoot invariants to the non-hardened path identical to those already enforced for `hardenedCalc` in `ExchangeCapsule.transaction()`.
- Add fuzz/property tests asserting the swap output never exceeds the counter-token reserve for extreme balance ratios and quantities, for both processors.

### Proof of Concept
Not directly executable from indexed context (no visibility into current mainnet fork/proposal state controlling `allowHarden()`/`allowStrictMath()`). Conceptually: craft an `Exchange` pool with a highly skewed `firstTokenBalance`/`secondTokenBalance` ratio, then issue an `ExchangeTransactionContract` sized so that `ExchangeProcessor`'s double-precision Bancor formula truncation yields `buyTokenQuant` exceeding the amount the exact/hardened formula would return; repeat until the pool's counter-asset reserve is drained beyond its backed value, as demonstrated by the discrepancy `ExchangeProcessorTest.testStrictMath` already asserts between the two processors [4](#0-3) .

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L67-69)
```java
      byte[] anotherTokenID;
      long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
          dynamicStore.allowStrictMath(), allowHarden());
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L199-205)
```java
    long balanceLimit = dynamicStore.getExchangeBalanceLimit();
    long tokenBalance = (Arrays.equals(tokenID, firstTokenID) ? firstTokenBalance
        : secondTokenBalance);
    tokenBalance = addExact(tokenBalance, tokenQuant);
    if (tokenBalance > balanceLimit) {
      throw new ContractValidateException("token balance must less than " + balanceLimit);
    }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-158)
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
```

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L160-162)
```java
    if (hardenedCalc && (newFirstTokenBalance < 0 || newSecondTokenBalance < 0)) {
      throw new ContractValidateException("Exchange balance must be >=0 after transaction");
    }
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
