### Title
Non-hardened Bancor-relay exchange calculation permits floating-point rounding drift that lets repeated `ExchangeTransactionContract` swaps permanently drain a TRC10/TRX Exchange pool - (File: `chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java`)

### Summary
`ExchangeTransactionActuator.execute()` calls `ExchangeCapsule.transaction()`, which, unless the `allowHardenExchangeCalculation` hard-fork switch is active, instantiates a plain `ExchangeProcessor` that computes the Bancor-style relay conversion with `double`/`Math.pow` arithmetic instead of exact integer/BigDecimal math used by `SafeExchangeProcessor`. Any unprivileged account can repeatedly submit `ExchangeTransactionContract` transactions (a normal signed transaction, fully reachable via `Wallet`/gRPC/HTTP) to exploit the accumulated floating point rounding in `exchangeToSupply`/`exchangeFromSupply`, similar in spirit to the CashCowCoin incident where a privileged reserve-adjustment path let ~80 iterative sell cycles drain the pool's reserve without a correctly reconciled invariant.

### Finding Description
`ExchangeCapsule.transaction()` re-derives the constant-supply Bancor formula on every call using a fresh `ExchangeProcessor` (or `SafeExchangeProcessor` when hardened) instantiated with a fixed `supply = 1_000_000_000_000_000_000L`: [1](#0-0) 

The default (non-hardened) path uses `Math.pow`-based double arithmetic in `ExchangeProcessor.exchangeToSupply`/`exchangeFromSupply`: [2](#0-1) 

This is gated only by the `allowHardenExchangeCalculation` dynamic property, checked in `AbstractExchangeActuator.allowHarden()`: [3](#0-2) 

The existence of `SafeExchangeProcessor`, which recomputes the same math with `BigDecimal` for exactness and explicitly guards against overshoot beyond the buy reserve (`testSafeProcessorNoOvershootForTypicalInputs`), together with `ExchangeProcessorTest.testStrictMath` explicitly asserting that the non-strict-math and strict-math paths produce *different* results for the exact same inputs, confirms that the default floating-point path is numerically imprecise and can diverge from the reserve-safe exact result: [4](#0-3) [5](#0-4) 

`ExchangeTransactionActuator.execute()` is the reachable entry point that any unprivileged account can call by signing an `ExchangeTransactionContract` transaction and broadcasting it; the actuator's `validate()` only performs bounds/precision sanity checks against `dynamicStore.getExchangeBalanceLimit()` and a `tokenExpected` slippage bound, it does not enforce that repeated small trades cannot exploit compounding rounding error over many iterations: [6](#0-5) [7](#0-6) 

This is structurally analogous to the CashCowCoin bug class: a repeated, low-friction operation (there: privileged burn+`sync()`, here: unprivileged repeated `ExchangeTransactionContract` swaps under the non-hardened math) breaks the AMM/bonding-curve invariant incrementally across iterations, each time extracting slightly more value than the exact formula would allow, permanently reducing the pool reserve relative to what the correct invariant requires.

### Impact Explanation
If `allowHardenExchangeCalculation` is not active on a given network (it is a dynamic/hard-fork-gated property, not always-on), any account can iteratively call `ExchangeTransactionContract` against a TRX/TRC10 Exchange pool and, by exploiting floating-point rounding asymmetries in `ExchangeProcessor`, extract more TRX/TRC10 from the pool reserves than the exact bonding-curve math would allow. Repeated over many iterations (analogous to the ~80 sell cycles in the report), this can drain the exchange pool's reserves, causing permanent loss of funds for the pool/liquidity owner and other traders — a concrete "theft or permanent freezing of funds" outcome.

### Likelihood Explanation
Medium. `ExchangeTransactionContract` is a standard, unprivileged, broadcastable transaction type reachable from any signed transaction, with no special permissions required beyond owning the tokens being sold. Exploitability depends on the `allowHardenExchangeCalculation` flag being off (default/legacy chain state) and on the magnitude of floating-point drift being economically worthwhile per iteration, which requires precise selection of trade sizes; the existence of the separate hardened path and explicit tests distinguishing the two implementations' outputs indicates the floating-point path's imprecision is a recognized, real numerical difference rather than a purely theoretical one.

### Recommendation
Make `SafeExchangeProcessor` (BigDecimal-exact, overshoot-checked) the unconditional implementation for `ExchangeCapsule.transaction()` regardless of `allowHardenExchangeCalculation`, or activate the hardening flag network-wide, removing the legacy double/`Math.pow` code path entirely so that no unprivileged transaction can trigger the imprecise arithmetic.

### Proof of Concept
1. On a chain/branch where `allowHardenExchangeCalculation` is not activated, create an Exchange pool via `ExchangeCreateContract` with TRX and a TRC10 token.
2. Repeatedly submit small `ExchangeTransactionContract` transactions (`ExchangeTransactionActuator.execute()` → `ExchangeCapsule.transaction()` → `ExchangeProcessor.exchange()`), each within slippage tolerance (`tokenExpected`).
3. Because `ExchangeProcessor.exchangeToSupply`/`exchangeFromSupply` use `double` arithmetic with `Math.pow`, verified in `ExchangeProcessorTest.testStrictMath` to differ from the exact `SafeExchangeProcessor` result for identical inputs, repeated iterations accumulate a rounding bias that extracts more of the reserve than the invariant should allow, mirroring the iterative-drain pattern described in the external report.
4. Compare final pool reserves after N iterations against the reserves predicted by `SafeExchangeProcessor` for the same sequence of trades to demonstrate the discrepancy/drain.

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L38-99)
```java
  @Override
  public boolean execute(Object object) throws ContractExeException {
    TransactionResultCapsule ret = (TransactionResultCapsule) object;
    if (Objects.isNull(ret)) {
      throw new RuntimeException(ActuatorConstant.TX_RESULT_NULL);
    }

    long fee = calcFee();
    AccountStore accountStore = chainBaseManager.getAccountStore();
    DynamicPropertiesStore dynamicStore = chainBaseManager.getDynamicPropertiesStore();
    ExchangeStore exchangeStore = chainBaseManager.getExchangeStore();
    ExchangeV2Store exchangeV2Store = chainBaseManager.getExchangeV2Store();
    AssetIssueStore assetIssueStore = chainBaseManager.getAssetIssueStore();
    try {
      final ExchangeTransactionContract exchangeTransactionContract = this.any
          .unpack(ExchangeTransactionContract.class);
      AccountCapsule accountCapsule = accountStore
          .get(exchangeTransactionContract.getOwnerAddress().toByteArray());

      ExchangeCapsule exchangeCapsule = Commons
          .getExchangeStoreFinal(dynamicStore, exchangeStore, exchangeV2Store)
          .get(ByteArray.fromLong(exchangeTransactionContract.getExchangeId()));

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L149-182)
```java
    if (!accountStore.has(ownerAddress)) {
      throw new ContractValidateException("account[" + readableOwnerAddress + NOT_EXIST_STR);
    }

    AccountCapsule accountCapsule = accountStore.get(ownerAddress);

    if (accountCapsule.getBalance() < calcFee()) {
      throw new ContractValidateException("No enough balance for exchange transaction fee!");
    }

    ExchangeCapsule exchangeCapsule;
    try {
      exchangeCapsule = Commons.getExchangeStoreFinal(dynamicStore, exchangeStore, exchangeV2Store)
          .get(ByteArray.fromLong(contract.getExchangeId()));
    } catch (ItemNotFoundException ex) {
      throw new ContractValidateException("Exchange[" + contract.getExchangeId()
          + ActuatorConstant.NOT_EXIST_STR);
    }

    byte[] firstTokenID = exchangeCapsule.getFirstTokenId();
    byte[] secondTokenID = exchangeCapsule.getSecondTokenId();
    long firstTokenBalance = exchangeCapsule.getFirstTokenBalance();
    long secondTokenBalance = exchangeCapsule.getSecondTokenBalance();

    byte[] tokenID = contract.getTokenId().toByteArray();
    long tokenQuant = contract.getQuant();
    long tokenExpected = contract.getExpected();

    if (dynamicStore.getAllowSameTokenName() == 1
        && !Arrays.equals(tokenID, TRX_SYMBOL_BYTES)
        && !isNumber(tokenID)) {
      throw new ContractValidateException("token id is not a valid number");
    }
    if (!Arrays.equals(tokenID, firstTokenID) && !Arrays.equals(tokenID, secondTokenID)) {
```
