### Title
Floating-point (`double`) arithmetic in `ExchangeProcessor` produces imprecise/inconsistent Bancor-formula token amounts, analogous to the incorrect price-averaging math in the reported oracle bug - (File: `chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java`)

### Summary
The reported bug is a pure math/formula error in an on-chain price calculation (`StableOracleDAI.sol:getPriceUSD()`) that silently returns a wrong USD/DAI rate, corrupting downstream minting and rebalancing amounts. The closest reachable analog in java-tron is `ExchangeProcessor`, the default TRC10 bonding-curve ("Bancor") exchange calculator used by `ExchangeTransactionActuator`, `ExchangeInjectActuator`, and `ExchangeWithdrawActuator` when the exchange is executed without the hardened path. It computes the amount of tokens to mint/burn using `double`-precision `Math.pow` operations instead of exact arithmetic, which — like the DAI price formula — can silently produce a materially wrong "price"/exchange amount for a transaction that any account can broadcast.

### Finding Description
`ExchangeCapsule.transaction()` selects between `ExchangeProcessor` (double-based) and `SafeExchangeProcessor` (BigDecimal-based) depending on the `hardenedCalc` flag/`AllowHardenExchangeCalculation` proposal: [1](#0-0) 

The unhardened `ExchangeProcessor` computes the bonding-curve output entirely with `double` and `Maths.pow`: [2](#0-1) 

This is structurally the same bug class as the external report: a formula meant to derive a token's exchange "price" is computed with an approximation (fixed-point averaging of two price feeds in the Solidity report; `double`/`Math.pow` floating point in java-tron) instead of an exact calculation, and the result is used directly to move real value (mint TRC10 supply, transfer TRX/TRC10 balances) via `ExchangeInjectActuator.execute()` and `ExchangeWithdrawActuator.execute()`, both reachable directly from a broadcast `ExchangeInjectContract`/`ExchangeWithdrawContract`/`ExchangeTransactionContract` transaction: [3](#0-2) 

The codebase itself acknowledges the risk by introducing `SafeExchangeProcessor`, a `BigDecimal`-based exact replacement, gated behind `AllowHardenExchangeCalculation`: [4](#0-3) 

The presence of this hardened alternative confirms that the original `double`-based path is recognized as imprecise/exploitable, but it remains the default unless a chain-wide proposal enables the harden flag, and the actuators still call `useStrictMath` variants of `ExchangeProcessor` rather than always using `SafeExchangeProcessor`.

### Impact Explanation
Floating-point `Math.pow`/`double` computations are not exact and can diverge from the "true" bonding-curve output, especially near edge ratios or for large balances, in a way that is deterministic per JVM/build but mathematically incorrect relative to the intended formula (mirroring how the audited DAI price formula returned a systematically wrong number rather than throwing an error). Because the output of `exchangeFromSupply`/`exchangeToSupply` directly determines how many tokens a user receives from `ExchangeInjectActuator`, `ExchangeWithdrawActuator`, or `ExchangeTransactionActuator`, a consistent rounding/precision bias can be repeatedly exploited by any account performing exchange transactions to extract more value than deposited, gradually draining an exchange pool's TRX/TRC10 balances (unbacked balance / theft-of-funds class impact).

### Likelihood Explanation
Any unprivileged account can broadcast `ExchangeInjectContract`, `ExchangeWithdrawContract`, or `ExchangeTransactionContract` transactions, and the unhardened `ExchangeProcessor` path is the historical default (before `AllowHardenExchangeCalculation` is turned on by committee proposal). Repeated small transactions accumulating rounding bias require no special privileges, only the ability to submit transactions and hold/acquire TRC10 balances, matching the "unprivileged transaction broadcaster" reachability bar.

### Recommendation
Make `SafeExchangeProcessor` (exact `BigDecimal` arithmetic) the only exchange processor, or ensure `AllowHardenExchangeCalculation` is unconditionally enabled and the `ExchangeProcessor` double-based path is fully retired, eliminating the floating-point precision risk in all exchange actuators (`ExchangeInjectActuator`, `ExchangeWithdrawActuator`, `ExchangeTransactionActuator`).

### Proof of Concept
Not independently reproduced in this analysis — this assessment is based on static code review of `ExchangeProcessor.java`, `SafeExchangeProcessor.java`, and `ExchangeCapsule.java`. I was unable to fully trace `MarketUtils.multiplyAndDivide` (used in `MarketSellAssetActuator`) in the time available, so I cannot confirm or rule out a similar issue there; this should be verified separately if pursued further.

### Citations

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L59-83)
```java
          .get(ByteArray.fromLong(exchangeInjectContract.getExchangeId()));
      byte[] firstTokenID = exchangeCapsule.getFirstTokenId();
      byte[] secondTokenID = exchangeCapsule.getSecondTokenId();
      long firstTokenBalance = exchangeCapsule.getFirstTokenBalance();
      long secondTokenBalance = exchangeCapsule.getSecondTokenBalance();

      byte[] tokenID = exchangeInjectContract.getTokenId().toByteArray();
      long tokenQuant = exchangeInjectContract.getQuant();

      byte[] anotherTokenID;
      long anotherTokenQuant;

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

**File:** chainbase/src/main/java/org/tron/core/capsule/SafeExchangeProcessor.java (L19-44)
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

  @Override
  public long exchange(long sellTokenBalance, long buyTokenBalance, long sellTokenQuant) {
    BigDecimal relay = exchangeToSupply(sellTokenBalance, sellTokenQuant);
    return exchangeFromSupply(buyTokenBalance, relay);
  }
```
