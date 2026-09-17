### Title
Unvalidated legacy Bancor exchange-rate calculation allows unsanitized "price" to corrupt exchange pool and account balances - (File: `chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java`)

### Summary
`ExchangeTransactionActuator` lets any account trade against a TRC10 Bancor-style liquidity pool by calling `ExchangeCapsule.transaction()`, which computes an exchange "price" (`buyTokenQuant`) via `ExchangeProcessor` (double-precision floating point `pow`) when the hardened path is not enabled. Unlike the Pyth report, here the untrusted "oracle-like" value is the *computed* exchange rate/output amount itself, and the legacy code path performs no sanity checks (`>0`, no overflow, resulting balances `>=0`) on that computed value before mutating account and pool balances, mirroring the missing `price/expo/conf` validation described in the report.

### Finding Description
`ExchangeTransactionActuator.execute()` calls `ExchangeCapsule.transaction(tokenID, tokenQuant, dynamicStore.allowStrictMath(), allowHarden())` to compute `anotherTokenQuant`, then directly applies it to both the exchange pool (`Commons.putExchangeCapsule`) and the trader's/pool's account balances via `addAssetAmountV2`/`reduceAssetAmountV2` and `setBalance` [1](#0-0) .

Inside `ExchangeCapsule.transaction()`, when `hardenedCalc` is `false` (the legacy/default `ExchangeProcessor` path), the resulting `newFirstTokenBalance`/`newSecondTokenBalance` and `buyTokenQuant` are used with **no bounds or sign validation** — the `if (hardenedCalc && (newFirstTokenBalance < 0 || newSecondTokenBalance < 0))` guard only fires when `hardenedCalc` is true [2](#0-1) .

`ExchangeProcessor` computes the trade output using raw `double` arithmetic and `Math.pow`-style exponentiation (`Maths.pow(1.0 + (double) quant / newBalance, 0.0005, ...)` and `Maths.pow(1.0 + (double) supplyQuant / supply, 2000.0, ...)`), casting the result to `long` with an implicit truncation and no post-computation validation [3](#0-2) . By contrast, the newer `SafeExchangeProcessor` (used only when `hardenedCalc` is true) performs the same computation with `BigDecimal` and is the version that actually checks that resulting balances stay non-negative [4](#0-3) .

The only guard in the actuator's `doValidate()` is `anotherTokenQuant < tokenExpected` — but `tokenExpected` is attacker-controlled input from the same transaction (`ExchangeTransactionContract.getExpected()`), so an attacker can simply set `tokenExpected` to a low/degenerate value that still passes even if `anotherTokenQuant` is a corrupted, floating-point-derived number resulting from extreme pool ratios (e.g., very large `sellTokenQuant` relative to a small pool `secondTokenBalance`, or repeated trades that drive `newBalance` toward extreme skew) [5](#0-4) . This is functionally identical to the reported bug class: a numeric "price" value produced by an external/complex computation is consumed and applied to fund transfers without validating that it is sane (non-negative, within expected bounds, free of floating-point precision corruption).

### Impact Explanation
Because the legacy `ExchangeProcessor` path is reachable by any unprivileged account issuing an `ExchangeTransactionContract` (broadcastable transaction, no special permission required), and its floating-point-derived trade output is trusted without bounds checking, a crafted sequence of trades against a thin/skewed liquidity pool can produce an exchange rate that under- or over-credits/-debits TRC10 asset or TRX balances relative to the true Bancor curve. This can result in unbacked asset balances being credited to an attacker's account or the pool's balances being driven inconsistent with real reserves — i.e., theft of value or permanent corruption/freezing of the exchange pool's accounting, which affects `AccountStore` balances used for all subsequent trades.

### Likelihood Explanation
Moderate to high: `ExchangeTransactionContract` is a normal, low-fee (`calcFee()==0`), unprivileged transaction type any account can submit; exploitation requires no special permissions, only control over `tokenId`, `quant`, and `expected` fields plus the ability to manipulate/observe pool state through prior trades (also unprivileged). Whether `hardenedCalc`/`allowHarden()` is enabled on the target network via governance proposal determines which processor executes, and only the un-hardened branch lacks the balance-sanity check — meaning this defect's exploitability is contingent on the network's current `allowHarden()` proposal state, which I was not able to fully confirm from the available context (only that it is configured via `DynamicPropertiesStore`/`ProposalUtil` and defaults are set through the maintenance/proposal mechanism, not verified as on/off in this codebase snapshot).

### Recommendation
Apply the `SafeExchangeProcessor`-style non-negative balance and bounds validation (and ideally overflow-safe `BigDecimal` computation) unconditionally in `ExchangeCapsule.transaction()`, regardless of `hardenedCalc`, so that any computed `buyTokenQuant`/resulting pool balances are validated for `> 0` and `>= 0` before being persisted, and reject the transaction (`ContractValidateException`) if the computed exchange output is inconsistent with pool invariants — mirroring the recommended `price <= 0` / bounds checks from the referenced report.

### Proof of Concept
Conceptual (network-state dependent, since exact reachability depends on `allowHarden()`/`allowStrictMath()` proposal values):
1. Attacker creates/uses an existing TRC10↔TRX (or TRC10↔TRC10) `Exchange` with a small `secondTokenBalance`.
2. Attacker submits an `ExchangeTransactionContract` selling a `tokenQuant` chosen to push `exchangeToSupply`/`exchangeFromSupply` double-precision computations into a region where truncation/precision loss yields an inflated or deflated `buyTokenQuant` relative to the true curve.
3. Attacker sets `expected` to a value low enough to pass `anotherTokenQuant < tokenExpected` in `doValidate()` [5](#0-4) .
4. `execute()` applies the unvalidated `anotherTokenQuant` to account/pool balances [6](#0-5) , resulting in balances inconsistent with the true Bancor invariant.

I was unable to directly confirm the current default/active value of `allowHarden()` (governance proposal state) in this snapshot, which affects whether the vulnerable legacy branch is actively used on mainnet versus the hardened branch; this should be verified before treating this as an actively exploitable production issue.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L66-97)
```java

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L217-221)
```java
    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
    }
```

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
