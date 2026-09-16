### Title
Floating-point precision manipulation in the TRC10 bancor-style Exchange rate calculation allows fund drain via `ExchangeTransactionContract` - (File: `chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java`)

### Summary
The external report describes an AMM/lending protocol where an unprivileged user can manipulate a token exchange rate to steal assets from the pool. In java-tron, the analogous surface is the native TRC10/TRX bancor-relay `Exchange` system (`ExchangeCreateContract`/`ExchangeInjectContract`/`ExchangeTransactionContract`/`ExchangeWithdrawContract`), reachable by any signed transaction from any account. The core rate math (`ExchangeProcessor`) is implemented with IEEE-754 `double` arithmetic and truncating casts to `long`, rather than the exact `BigDecimal`/`SafeExchangeProcessor` implementation that the codebase itself introduces as a "hardened" alternative. Because the hardened, precise path is opt-in (gated by a flag passed into `ExchangeCapsule.transaction(...)`), the default computation path is exposed to floating-point rounding/precision manipulation across repeated, otherwise legitimate `ExchangeTransactionContract` calls, which is directly analogous to the reported "attacker-controlled zero/degenerate exchange rate" bug class.

### Finding Description
`ExchangeTransactionActuator.execute()` calls `exchangeCapsule.transaction(tokenID, tokenQuant, dynamicStore.allowStrictMath(), allowHarden())` [1](#0-0) , which dispatches to either the legacy double-based `ExchangeProcessor` or the newer `SafeExchangeProcessor` depending on the `hardenedCalc` flag [2](#0-1) .

The default/legacy processor computes the bancor-relay exchange using `double` math and truncates to `long` via a raw cast: [3](#0-2) 

This differs from the hardened implementation, which performs the same computation using `BigDecimal` with explicit scale/rounding control: [4](#0-3) 

Because any account can freely call `ExchangeTransactionContract` (subject only to generic "quant/expected > 0" and balance-limit checks in `ExchangeTransactionActuator.doValidate()` [5](#0-4) , an attacker can choose adversarial `sellTokenQuant` values (including very small amounts against a pool previously thinned out via `ExchangeWithdrawActuator`) to repeatedly exploit `double`-precision truncation/rounding in `exchangeToSupply`/`exchangeFromSupply`. Repeated calls compound rounding bias in the attacker's favor, letting them extract more of the counter-asset than the exact bancor formula would allow, effectively distorting the pool's implied "exchange rate" in the same way the external report describes an attacker forcing `exchangeRate = 0` to extract assets disproportionately to what was deposited.

The project's own test suite acknowledges this class of risk by adding `allowHarden()`/`SafeExchangeProcessor` and by adding explicit overflow/precision tests (`hardenedSuccessExchangeTransaction`, `hardenedExecuteOverflowThrowsArithmeticException`) [6](#0-5) , but the un-hardened default path still exists as the fallback whenever the hardened calculation flag is not active for a given transaction.

### Impact Explanation
If the hardened path is not universally enforced, any unprivileged account holding a small amount of a listed TRC10/TRX pair can call `ExchangeTransactionContract` (and combine it with `ExchangeInjectContract`/`ExchangeWithdrawContract` to manipulate pool reserve ratios beforehand) to extract more value than deposited, draining counterparty liquidity from the AMM pool — a direct unauthorized transfer/theft of pooled TRX/TRC10 assets, matching the "unbacked balance / theft of funds" impact bar.

### Likelihood Explanation
Reachable directly and repeatedly by any account via a single broadcast transaction type (`ExchangeTransactionContract`), with only generic quantity/limit validation and no protection against floating-point degeneracies in `ExchangeProcessor`. No special privileges, contract deployment, or malicious-SR/witness assumptions are needed — it is purely a client/transaction-broadcaster-reachable exploit path through `ExchangeTransactionActuator` → `ExchangeCapsule.transaction()` → `ExchangeProcessor`.

### Recommendation
Make the `SafeExchangeProcessor` (`BigDecimal`-based, exact rounding) the unconditional/default computation path for all `Exchange*` actuators instead of gating it behind an opt-in "hardened" flag, so floating-point precision cannot be leveraged to distort the bancor exchange rate. Additionally, add invariant checks after every exchange (e.g., constant-product/relay-supply invariants) that reject a transaction if the resulting reserve ratio deviates from the mathematically exact value beyond a strict epsilon.

### Proof of Concept
Not independently executable from static analysis alone; conceptually: (1) create/observe a low-liquidity Exchange pool (via `ExchangeCreateContract`/`ExchangeWithdrawContract` to thin reserves), (2) repeatedly invoke `ExchangeTransactionContract` with `token_id`/`quant` chosen so that `ExchangeProcessor.exchangeToSupply`/`exchangeFromSupply`'s double-to-long truncation biases in the caller's favor each time, (3) observe that the attacker's cumulative extracted counter-token amount exceeds what the exact (`SafeExchangeProcessor`) computation would allow, at the expense of other pool participants. I could not confirm from the index whether `AllowHardenExchangeCalculation` is enabled by default network-wide (the property definition was not found within available search results in `DynamicPropertiesStore.java`), so confirming exploitability at HIGH vs MEDIUM severity would require checking the live chain parameter value and reproducing the drift numerically in a test harness — this would need a Devin session with full repo/test access to validate precisely.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L67-69)
```java
      byte[] anotherTokenID;
      long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
          dynamicStore.allowStrictMath(), allowHarden());
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L186-221)
```java
    if (tokenQuant <= 0) {
      throw new ContractValidateException("token quant must greater than zero");
    }

    if (tokenExpected <= 0) {
      throw new ContractValidateException("token expected must greater than zero");
    }

    if (firstTokenBalance == 0 || secondTokenBalance == 0) {
      throw new ContractValidateException("Token balance in exchange is equal with 0,"
          + "the exchange has been closed");
    }

    long balanceLimit = dynamicStore.getExchangeBalanceLimit();
    long tokenBalance = (Arrays.equals(tokenID, firstTokenID) ? firstTokenBalance
        : secondTokenBalance);
    tokenBalance = addExact(tokenBalance, tokenQuant);
    if (tokenBalance > balanceLimit) {
      throw new ContractValidateException("token balance must less than " + balanceLimit);
    }

    if (Arrays.equals(tokenID, TRX_SYMBOL_BYTES)) {
      if (accountCapsule.getBalance() < addExact(tokenQuant, calcFee())) {
        throw new ContractValidateException("balance is not enough");
      }
    } else {
      if (!accountCapsule.assetBalanceEnoughV2(tokenID, tokenQuant, dynamicStore)) {
        throw new ContractValidateException("token balance is not enough");
      }
    }

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

**File:** framework/src/test/java/org/tron/core/actuator/ExchangeTransactionActuatorTest.java (L1832-1907)
```java
  /**
   * Hardened mode: ExchangeTransaction succeeds and routes through SafeExchangeProcessor.
   */
  @Test
  public void hardenedSuccessExchangeTransaction() {
    dbManager.getDynamicPropertiesStore().saveAllowSameTokenName(1);
    dbManager.getDynamicPropertiesStore().saveAllowHardenExchangeCalculation(1);
    InitExchangeSameTokenNameActive();
    long exchangeId = 1;
    String tokenId = "_";
    long quant = 100_000_000L;

    byte[] ownerAddress = ByteArray.fromHexString(OWNER_ADDRESS_SECOND);
    AccountCapsule before = dbManager.getAccountStore().get(ownerAddress);
    long initialBalance = before.getBalance();

    ExchangeTransactionActuator actuator = new ExchangeTransactionActuator();
    actuator.setChainBaseManager(dbManager.getChainBaseManager()).setAny(getContract(
        OWNER_ADDRESS_SECOND, exchangeId, tokenId, quant, 1));

    TransactionResultCapsule ret = new TransactionResultCapsule();
    try {
      actuator.validate();
      actuator.execute(ret);
      Assert.assertEquals(code.SUCESS, ret.getInstance().getRet());
      AccountCapsule after = dbManager.getAccountStore().get(ownerAddress);
      Assert.assertEquals(initialBalance - quant, after.getBalance());
      Assert.assertTrue("Hardened tx must produce positive received amount",
          ret.getExchangeReceivedAmount() > 0);
    } catch (Exception e) {
      Assert.fail("Hardened transaction must succeed: " + e.getMessage());
    } finally {
      dbManager.getExchangeStore().delete(ByteArray.fromLong(1L));
      dbManager.getExchangeStore().delete(ByteArray.fromLong(2L));
      dbManager.getExchangeV2Store().delete(ByteArray.fromLong(1L));
      dbManager.getExchangeV2Store().delete(ByteArray.fromLong(2L));
      dbManager.getDynamicPropertiesStore().saveAllowHardenExchangeCalculation(0);
    }
  }

  /**
   * Hardened mode: corrupt pool with near-MAX balance triggers ArithmeticException
   * from addExact. Demonstrates the overflow-detection guard fires and is not
   * silently swallowed.
   */
  @Test
  public void hardenedExecuteOverflowThrowsArithmeticException() throws Exception {
    dbManager.getDynamicPropertiesStore().saveAllowSameTokenName(1);
    dbManager.getDynamicPropertiesStore().saveAllowHardenExchangeCalculation(1);
    InitExchangeSameTokenNameActive();

    long exchangeId = 1;
    // Corrupt pool to near-MAX TRX so addExact overflows when buying.
    ExchangeCapsule pool = dbManager.getExchangeV2Store().get(ByteArray.fromLong(exchangeId));
    pool.setBalance(Long.MAX_VALUE - 5L, 10_000_000L);
    dbManager.getExchangeV2Store().put(pool.createDbKey(), pool);

    String tokenId = "_";
    long quant = 100L;
    ExchangeTransactionActuator actuator = new ExchangeTransactionActuator();
    actuator.setChainBaseManager(dbManager.getChainBaseManager()).setAny(getContract(
        OWNER_ADDRESS_SECOND, exchangeId, tokenId, quant, 1));

    try {
      // addExact throws ArithmeticException, which is wrapped into ContractExeException.
      Assert.assertThrows(ContractExeException.class,
          () -> actuator.execute(new TransactionResultCapsule()));
    } finally {
      dbManager.getExchangeStore().delete(ByteArray.fromLong(1L));
      dbManager.getExchangeStore().delete(ByteArray.fromLong(2L));
      dbManager.getExchangeV2Store().delete(ByteArray.fromLong(1L));
      dbManager.getExchangeV2Store().delete(ByteArray.fromLong(2L));
      dbManager.getDynamicPropertiesStore().saveAllowHardenExchangeCalculation(0);
    }
  }
}
```
