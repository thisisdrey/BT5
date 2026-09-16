### Title
Non-deterministic / unguarded Bancor-style pricing in `ExchangeTransactionActuator` when legacy (non-hardened, non-strict) math processor is configured - ([File: chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java])

### Summary
`ExchangeCapsule#transaction()` selects between two mathematically distinct pricing engines for the on-chain TRX/TRC10 Bancor-style Exchange based on the chain-governed flags `allowHardenExchangeCalculation` and `allowStrictMath`, exactly analogous to the reported "misconfigured `N_COINS`" issue: a single configuration parameter decides which price-calculation implementation is trusted, and the implementations are not provably equivalent for all pool states, yet the actual value-moving trade path (`ExchangeTransactionActuator`) applies none of the precision/sanity safeguards that the codebase itself later added to the sibling `ExchangeWithdrawActuator`/`ExchangeInjectActuator` paths.

### Finding Description
`ExchangeCapsule#transaction()` picks the processor purely from the `hardenedCalc` boolean: [1](#0-0) . The legacy `ExchangeProcessor` computes the Bancor relay/output amounts with plain `double` arithmetic and `Math.pow`/`StrictMath.pow` gated by a second flag `useStrictMath`, while `SafeExchangeProcessor` uses `BigDecimal` with fixed scale/rounding [2](#0-1) [3](#0-2) .

The project's own tests demonstrate these two code paths are not equivalent: `testStrictMath` explicitly asserts the non-strict and strict `double` outputs are `assertNotEquals` for identical inputs [4](#0-3) , and `testTransactionLegacyVsHardenedProcessorSelection` only tolerates the hardened vs. legacy result diverging by "±1 unit" rather than being identical [5](#0-4) . `ExchangeTransactionActuator`, which is the actuator that actually executes a trade against the pool, calls `exchangeCapsule.transaction()` directly in both `validate()` and `execute()` with no additional bound/relative-error check on the computed `anotherTokenQuant` [6](#0-5) [7](#0-6) .

By contrast, `ExchangeWithdrawActuator` and `ExchangeInjectActuator` were later hardened with an explicit "Not precise enough" 0.01%-relative-error sanity check comparing the ratio-based BigInteger/BigDecimal computation against the expected result, gated by the same `allowHarden()` flag [8](#0-7) . No equivalent sanity check exists for the Bancor curve trade path in `ExchangeTransactionActuator`/`ExchangeCapsule#transaction()` — i.e., the component that actually performs the price-sensitive swap has weaker guardrails than the components that merely inject/withdraw liquidity at a linear ratio, mirroring the audit finding where the oracle only misbehaves once an edge condition (there: depeg; here: an extreme/skewed pool balance ratio driving the legacy `double`-based `Math.pow` computation into a region of large relative error or platform-dependent non-determinism) is hit.

### Impact Explanation
If `allowHardenExchangeCalculation`/`allowStrictMath` remain in their legacy configuration (the pre-hardening default before committee activation), an unprivileged caller can drive an `Exchange` pool (via repeated `ExchangeTransactionContract` trades, which anyone with balance can broadcast) into a heavily skewed balance ratio. In that region:
- The `double`/`Math.pow`-based legacy processor can be significantly less precise than the BigDecimal path, and unlike `ExchangeWithdrawActuator`/`ExchangeInjectActuator`, `ExchangeTransactionActuator` has no relative-error safety check, so an imprecise/favorable-to-attacker output can be accepted and settled, permanently mispricing/draining the pool (unbacked balance / theft-of-funds class impact).
- Because `Math.pow` (non-strict) is explicitly *not* guaranteed bit-identical across JVMs/CPU architectures (which is exactly why the `useStrictMath` flag and `StrictMathWrapper` exist elsewhere in the codebase), executing this code during `execute()` — part of deterministic block application in `Manager` — with `allowStrictMath=false` creates a path where different full nodes could compute different `anotherTokenQuant` for the same transaction, risking state/chain divergence.

### Likelihood Explanation
Reaching this path requires only a signed `ExchangeTransactionContract` transaction against an existing `Exchange`/`ExchangeV2` pool — no special privilege is needed [9](#0-8) . The triggering condition (legacy math flags + extreme pool ratio) depends on network configuration and pool state rather than attacker-controlled code, similar to the original finding where the bug is latent until a specific market condition occurs.

### Recommendation
Apply the same "Not precise enough" style relative-error/consistency check used in `ExchangeWithdrawActuator`/`ExchangeInjectActuator` to the Bancor-curve trade path in `ExchangeCapsule#transaction()`/`ExchangeTransactionActuator`, and require `allowStrictMath` (deterministic `StrictMath`) whenever the legacy processor is used, or retire the legacy non-hardened/non-strict processor entirely so a single, provably deterministic and precision-bounded pricing implementation is used for all Exchange operations, closing the "which formula is trusted" configuration gap analogous to the oracle `N_COINS` misconfiguration.

### Proof of Concept
1. Deploy/observe an `Exchange` pool where `allowHardenExchangeCalculation()`/`allowStrictMath()` are both `false` (legacy path) as read via `AbstractExchangeActuator#allowHarden()` [10](#0-9)  and `dynamicStore.allowStrictMath()`.
2. Submit repeated `ExchangeTransactionContract` trades to skew `firstTokenBalance`/`secondTokenBalance` toward an extreme ratio (e.g., very large vs. very small, akin to the "one coin depegs" scenario in the source report).
3. Compare the `double`-based `ExchangeProcessor.exchange()` output at that ratio against the `BigDecimal`-based `SafeExchangeProcessor.exchange()`/exact-ratio result for the same balances/quant, as the test suite already shows measurable divergence between the two implementations [11](#0-10) .
4. Because `ExchangeTransactionActuator` performs no bound check on the resulting `anotherTokenQuant` (unlike `ExchangeWithdrawActuator`), the imprecise/divergent amount is settled directly into account balances, demonstrating the unguarded misconfiguration-dependent pricing path.

### Citations

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-129)
```java
  public long transaction(byte[] sellTokenID, long sellTokenQuant, boolean useStrictMath,
      boolean hardenedCalc) throws ContractValidateException {
    long supply = 1_000_000_000_000_000_000L;
    Processor processor = hardenedCalc
        ? SafeExchangeProcessor.INSTANCE : new ExchangeProcessor(supply, useStrictMath);

```

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java (L17-39)
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
```

**File:** chainbase/src/main/java/org/tron/core/capsule/SafeExchangeProcessor.java (L19-38)
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
```

**File:** framework/src/test/java/org/tron/core/capsule/utils/ExchangeProcessorTest.java (L218-230)
```java
  @Test
  public void testStrictMath() {
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

**File:** framework/src/test/java/org/tron/core/capsule/ExchangeCapsuleTest.java (L85-106)
```java
  @Test
  public void testTransactionLegacyVsHardenedProcessorSelection() throws Exception {
    // Same input produces deterministic results in both modes.
    ExchangeCapsule legacy = new ExchangeCapsule(
        ByteString.copyFromUtf8("owner"), 100L, 0L,
        "abc".getBytes(), "def".getBytes());
    legacy.setBalance(100_000_000L, 100_000_000L);
    long legacyResult = legacy.transaction("abc".getBytes(), 1_000_000L, true, false);

    ExchangeCapsule hardened = new ExchangeCapsule(
        ByteString.copyFromUtf8("owner"), 101L, 0L,
        "abc".getBytes(), "def".getBytes());
    hardened.setBalance(100_000_000L, 100_000_000L);
    long hardenedResult = hardened.transaction("abc".getBytes(), 1_000_000L, true, true);

    Assert.assertTrue("Both must return positive", legacyResult > 0 && hardenedResult > 0);
    Assert.assertTrue("Hardened must not exceed pool",
        hardenedResult <= 100_000_000L);
    // Allow ±1 difference due to BigDecimal vs double precision
    Assert.assertTrue("Results should be within 1 unit",
        StrictMathWrapper.abs(legacyResult - hardenedResult) <= 1);
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L38-59)
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
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L61-75)
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
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L217-221)
```java
    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L228-243)
```java
      if (allowHarden) {
        BigDecimal remainder = bigSecondTokenBalance.multiply(bigTokenQuant)
            .divide(bigFirstTokenBalance, 4, RoundingMode.HALF_UP)
            .subtract(BigDecimal.valueOf(anotherTokenQuant));
        if (remainder.compareTo(
            BigDecimal.valueOf(anotherTokenQuant).multiply(new BigDecimal("0.0001"))) > 0) {
          throw new ContractValidateException("Not precise enough");
        }
      } else {
        double remainder = bigSecondTokenBalance.multiply(bigTokenQuant)
            .divide(bigFirstTokenBalance, 4, BigDecimal.ROUND_HALF_UP).doubleValue()
            - anotherTokenQuant;
        if (remainder / anotherTokenQuant > 0.0001) {
          throw new ContractValidateException("Not precise enough");
        }
      }
```

**File:** actuator/src/main/java/org/tron/core/actuator/AbstractExchangeActuator.java (L13-15)
```java
  protected boolean allowHarden() {
    return chainBaseManager.getDynamicPropertiesStore().allowHardenExchangeCalculation();
  }
```
