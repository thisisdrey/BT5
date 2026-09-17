### Title
Legacy floating-point Bancor exchange calculation allows precision-manipulation drain of TRC10/TRX exchange pools when the hardened calculation proposal is not enabled - ([File: chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java])

### Summary
The DDCoin report shows an attacker using a flash-loan-funded repeated-trade loop against a DEX-like `MarketPlace` contract whose internal accounting used unsafe, manipulable price math, letting the attacker repeatedly extract more value than deposited. In java-tron's built-in TRC10 "Exchange" (Bancor-style AMM) module, the same bug class exists: the default calculation engine, `ExchangeProcessor`, computes swap amounts with unchecked `double` arithmetic (`Math.pow`) and raw `+`/`-` operators with no overflow or negative-balance guard, and is only replaced by the safe, `BigDecimal`-based `SafeExchangeProcessor` when the `allowHardenExchangeCalculation` committee proposal has been activated on a given chain.

### Finding Description
`ExchangeCapsule.transaction()` selects the processor based on the `hardenedCalc` flag: [1](#0-0) . When `hardenedCalc` is `false` (the state of any chain/network that has not yet passed the `allowHardenExchangeCalculation` proposal), it instantiates the legacy `ExchangeProcessor`, whose bonding-curve math uses `double`/`Maths.pow` and plain `long` addition/subtraction with no `addExact`/`subtractExact` overflow checks and no floor on resulting balances: [2](#0-1) . Compare this to the hardened path, which uses `BigDecimal` with `RoundingMode.HALF_UP` and `StrictMathWrapper.addExact`/`subtractExact`, plus explicit `<0` checks in `ExchangeCapsule.transaction`: [3](#0-2) [4](#0-3) .

This mirrors the DDCoin root cause: an unsafe/manipulable pricing calculation reachable from an ordinary user transaction (`ExchangeTransactionActuator.execute`, invoked from any signed `ExchangeTransactionContract`) with no strict boundary checks on the resulting reserve balances. `ExchangeTransactionActuator.execute()` calls `exchangeCapsule.transaction(...)` directly, adjusts account balances, and persists the pool state without independently re-validating that reserves stay consistent when the non-hardened path is used: [5](#0-4) . Because floating-point `pow`/division introduces rounding error and truncation (`(long) issuedSupply`/`(long) exchangeBalance`), a series of small, carefully-sized trades (functionally equivalent to the attacker's repeated `sellItem`/`swapBUSDTToDD` loop in the DDCoin PoC) can accumulate rounding bias in the attacker's favor, letting them extract more of the paired asset than they contributed, draining the pool's TRX/TRC10 reserves over many transactions — the "unbacked balance"/fund-theft outcome called out in the DDCoin report, but achieved via cumulative precision abuse rather than a single flash-loan callback (java-tron has no smart-contract-invocable flash-loan-style callback into this actuator, so the attack must be executed as a sequence of ordinary `ExchangeTransactionContract` transactions instead of one atomic reentrant call).

### Impact Explanation
If exploited, an attacker can incrementally drain TRX or TRC10 tokens held in an Exchange pool beyond what they deposit, resulting in unauthorized asset transfer/theft of funds from the pool (and, transitively, from other liquidity providers who created/injected into the exchange via `ExchangeInjectActuator`/`ExchangeWithdrawActuator`). This is a direct financial loss to any TRC10 exchange pool that has not activated `allowHardenExchangeCalculation`.

### Likelihood Explanation
Exploitability depends entirely on whether the `allowHardenExchangeCalculation` proposal is active on the target chain/network. Per `ProposalUtil.java`/`DynamicPropertiesStore.java`, this is a committee-gated flag, meaning any chain (including private/side chains or a period before mainnet activation) that has not enabled it is exposed. Anyone with TRX/TRC10 balance and ability to submit `ExchangeTransactionContract` transactions against an existing exchange pair can attempt this — no special privilege is required, only patience/gas to run many trades and exploit rounding drift. The severity is bounded by pool size (`getExchangeBalanceLimit`) and by how much rounding bias can be extracted per trade, which needs off-chain modeling/experimentation to quantify precisely; this uncertainty is noted since dynamic default values for `allowHardenExchangeCalculation` and current mainnet activation status were not verified within this codebase snapshot.

### Recommendation
- Make `SafeExchangeProcessor` (or an equivalent overflow/rounding-safe implementation) the only calculation path, removing the legacy `ExchangeProcessor` double-based code path entirely, rather than gating it behind a proposal that some networks may never activate.
- Add explicit reserve-consistency invariants (e.g., product/curve invariant checks, or minimum-output slippage checks) in `ExchangeTransactionActuator.execute()` independent of `hardenedCalc`, so a manipulated calculation cannot silently reduce total reserve value.
- Add regression/fuzz tests that repeatedly trade small amounts against a pool under the legacy `ExchangeProcessor` to detect any net value extraction ("rounding dust" drain) over many iterations.

### Proof of Concept
A concrete PoC amount/iteration count could not be derived from static analysis alone; it requires running `ExchangeProcessor.exchange()` under many small `sellTokenQuant` values (with `allowHardenExchangeCalculation` disabled) to empirically measure whether the `(long) issuedSupply` / `(long) exchangeBalance` truncation in `exchangeToSupply`/`exchangeFromSupply` ( [6](#0-5) ) produces a statistically favorable rounding bias to the trader across repeated round-trip trades — analogous to how the DDCoin PoC looped `swapBUSDTToDD`/`sellItem` to accumulate stolen `BUSDT`. This would need to be validated empirically in a test harness (e.g., extending `framework/src/test/java/org/tron/core/actuator/ExchangeTransactionActuatorTest.java`) before being treated as confirmed rather than theoretical.

### Citations

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-129)
```java
  public long transaction(byte[] sellTokenID, long sellTokenQuant, boolean useStrictMath,
      boolean hardenedCalc) throws ContractValidateException {
    long supply = 1_000_000_000_000_000_000L;
    Processor processor = hardenedCalc
        ? SafeExchangeProcessor.INSTANCE : new ExchangeProcessor(supply, useStrictMath);

```

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L140-162)
```java
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

**File:** chainbase/src/main/java/org/tron/core/capsule/SafeExchangeProcessor.java (L17-44)
```java
  }

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

  @Override
  public long exchange(long sellTokenBalance, long buyTokenBalance, long sellTokenQuant) {
    BigDecimal relay = exchangeToSupply(sellTokenBalance, sellTokenQuant);
    return exchangeFromSupply(buyTokenBalance, relay);
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L61-99)
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

      ret.setExchangeReceivedAmount(anotherTokenQuant);
      ret.setStatus(fee, code.SUCESS);
```
