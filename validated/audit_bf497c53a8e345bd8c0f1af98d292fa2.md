### Title
Non-strict Bancor exchange math in `ExchangeTransactionActuator` uses floating-point `pow`/`long` truncation while validation and execution can diverge under default (non-hardened) math mode - ([File: actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java])

### Summary
The reported bug class is an AMM whose join/exit (or swap) math is computed with an approximate/asymmetric formula, and where a stale/rounded balance can be repeatedly exploited by an unprivileged caller performing many small operations to extract more value than deposited. The closest reachable analog in java-tron is the TRC10 **Exchange** module's swap path (`ExchangeTransactionActuator` → `ExchangeCapsule.transaction` → `ExchangeProcessor`), which is callable by **any** account (unlike `ExchangeInjectActuator`/`ExchangeWithdrawActuator`, which are restricted to the exchange creator) and which, by default, computes the Bancor-style relay/exchange amounts using **double-precision floating point `Math.pow`** and truncating `(long)` casts rather than exact integer/BigDecimal arithmetic.

### Finding Description
`ExchangeCapsule.transaction()` [1](#0-0)  selects between `ExchangeProcessor` (default) and `SafeExchangeProcessor` (only when `hardenedCalc`/`allowHarden()` is enabled) to price a swap between the two exchange reserves. In the default, non-hardened path, `ExchangeProcessor.exchangeToSupply`/`exchangeFromSupply` compute the swap output using `double` arithmetic and `Maths.pow(...)`, then truncate to `long` via a direct cast [2](#0-1) . This is fundamentally different from the exact `BigDecimal`/`longValueExact()` computations used by `ExchangeInjectActuator`/`ExchangeWithdrawActuator` validation paths for LP inject/withdraw, and from the hardened `SafeExchangeProcessor` which uses `BigDecimal` with explicit rounding modes and `RoundingMode.DOWN` truncation only at the final step [3](#0-2) .

`ExchangeTransactionActuator` is open to any account holding the traded token/TRX — there is no creator-only restriction as with inject/withdraw [4](#0-3) . Both `doValidate()` and `execute()` call `exchangeCapsule.transaction(...)` independently [5](#0-4) , meaning the pool state used at validation time is not the same pool state actually committed at execution time within a block (each call mutates `exchange.firstTokenBalance/secondTokenBalance` via `Commons.putExchangeCapsule`).

Because the default math relies on `double`/`Math.pow` and a raw truncating cast to `long` rather than well-defined integer rounding, repeated small trades in the same or opposite direction can accumulate floating-point/truncation drift between what the pool's tracked reserves say and the "true" invariant value, in the same conceptual way the Ocean BPool exploit chained many small single-sided operations to accumulate rounding-derived profit against an AMM's asymmetric join/exit math. This is architecturally analogous to (not identical to) the reported bug class: an externally reachable AMM-style balance-mutating actuator whose per-call math is not exact, invoked in a tight loop by an unprivileged caller.

However, I could not conclusively prove a concrete drainable profit path from the code alone — the actual exploitability depends on the magnitude of floating-point error versus the minimum tradable unit (`tokenQuant >= 1`), the `getExchangeBalanceLimit()` cap, and the `token required must greater than expected` slippage check in `doValidate()`, all of which bound but do not eliminate compounding rounding drift over many transactions. I was not able to construct or verify an end-to-end numeric PoC demonstrating net token creation (i.e., `firstTokenBalance + secondTokenBalance` increasing beyond what was deposited) purely from reading the source; this would require running the formula in `ExchangeProcessor` over a large number of iterations to confirm whether drift is strictly conservative (bounded to attacker's own loss) or can flip in the attacker's favor.

### Impact Explanation
If floating-point/truncation drift in `ExchangeProcessor.exchange()` can be steered to favor the caller across repeated calls, an attacker could extract more TRX/TRC10 tokens from an Exchange pool than they deposited, directly analogous to the Ocean BPool loss of ~127.86K mOCEAN — this would be a theft-of-funds / unbacked-balance issue affecting any TRC10 Exchange pool and its liquidity providers (the "victim" pools in the original report map to Exchange pool creators/depositors in java-tron). This would qualify as Medium/High impact (concrete theft of funds) if the drift is provably exploitable.

### Likelihood Explanation
Likelihood is **uncertain** given the available evidence. The attack surface is real and reachable by any unprivileged transaction sender (`ExchangeTransactionActuator` has no creator restriction, unlike inject/withdraw), and the default math path intentionally avoids the hardened `SafeExchangeProcessor`/`BigInteger` exact-division style used elsewhere in the same module family. However, without confirming the sign and magnitude of the floating-point error terms over many iterations, I cannot assert this is definitely exploitable — it might be that the error is bounded and always favors the pool (as intended by conservative truncation via `(long)` cast, which always rounds toward zero on the "issued"/"withdrawn" side). This uncertainty means I cannot fully validate root cause to the same rigor as the other analogs I might normally provide.

### Recommendation
Given the uncertainty in the numeric drift direction and magnitude, and the inability to construct a concrete PoC purely through static review, I do not have sufficient confidence to assert a definitive, provable vulnerability here matching the strict validation bar requested (concrete unauthorized theft demonstrated with exact math). Recommend that a background engineer or security reviewer:
1. Simulate `ExchangeProcessor.exchange()` over sequences of many small same/opposite-direction trades against fixed reserves to empirically determine if cumulative truncation ever produces net token creation (sum of reserves increasing) rather than net token destruction/dust loss.
2. Compare behavior with `allowHarden()`/`SafeExchangeProcessor` enabled vs disabled to confirm the hardened mode closes any drift, and confirm whether `allowHarden()` is enabled on mainnet by default.
3. If drift favoring the attacker is confirmed, apply the same `BigDecimal`/exact-integer approach used in `SafeExchangeProcessor` unconditionally in `ExchangeProcessor`, and enforce it as the default path rather than a governance-toggled hardened mode.

### Proof of Concept
Not constructed — I was unable to verify, through static code reading alone, whether repeated `ExchangeTransactionActuator` calls against `ExchangeProcessor`'s floating-point Bancor formula produce cumulative reserve inflation favoring the caller. This would require numerical simulation of `exchangeToSupply`/`exchangeFromSupply` (chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java, lines 17-45) across many iterations, which is outside the scope of static analysis performed here.

### Citations

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-169)
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L38-107)
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
    } catch (ItemNotFoundException | InvalidProtocolBufferException
        | ContractValidateException | ArithmeticException e) {
      logger.debug(e.getMessage(), e);
      ret.setStatus(fee, code.FAILED);
      throw new ContractExeException(e.getMessage());
    }
    return true;
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L217-220)
```java
    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
```
