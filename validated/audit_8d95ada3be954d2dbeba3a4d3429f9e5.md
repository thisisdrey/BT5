### Title
Legacy (non-hardened) Bancor-style Exchange reserve calculation lets an attacker push a token reserve to a floating-point-corrupted / near-zero value and grief or exploit subsequent `ExchangeTransaction`/`ExchangeInject`/`ExchangeWithdraw` transactions - ([File: actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java])

### Summary
The Hyperdrive report describes an attacker manipulating an AMM's reserve ratio to an extreme (near-zero) value by front-running, causing division-by-zero/underflow that DoSes a legitimate liquidity provider's transaction. java-tron's built-in `Exchange` feature (`ExchangeCreate`/`ExchangeInject`/`ExchangeWithdraw`/`ExchangeTransaction` contracts) implements an analogous bonding-curve AMM (`ExchangeCapsule.transaction`), and by default runs the legacy, floating-point `ExchangeProcessor` path rather than the hardened `SafeExchangeProcessor`, which has no protection against a reserve going to (or effectively becoming) zero/negative before persisting state.

### Finding Description
`ExchangeCapsule.transaction(...)` [1](#0-0)  selects between the legacy `ExchangeProcessor` (floating point, `double`-based bonding curve math) and `SafeExchangeProcessor` (BigDecimal-based, with an explicit non-negative balance check) depending on the `hardenedCalc` flag, which itself is controlled by `allowHardenExchangeCalculation()` — a chain parameter that defaults off unless enabled by a committee proposal [2](#0-1) .

In the legacy path, `ExchangeProcessor.exchangeToSupply`/`exchangeFromSupply` uses `double` arithmetic and `Maths.pow` [3](#0-2) . There is **no check that the resulting `buyTokenQuant` is less than the current `buyTokenBalance`**, and the negative-balance guard in `ExchangeCapsule.transaction` is only applied `if (hardenedCalc && ...)` [4](#0-3) . When `hardenedCalc` is false (the default), an attacker who submits an `ExchangeTransactionContract` with a large `sellTokenQuant` can drive `newFirstTokenBalance`/`newSecondTokenBalance` to an extremely small, zero, or even negative `long` value, and this corrupted state is persisted via `Commons.putExchangeCapsule(...)` [5](#0-4)  with only a generic try/catch around `ArithmeticException` — which double-based math does not throw.

Once one side of the pool reserve is at (or near) zero/negative, every subsequent unprivileged user's `ExchangeInjectActuator`, `ExchangeWithdrawActuator`, or `ExchangeTransactionActuator` call against that pool computes ratios directly from these corrupted balances:
- `ExchangeInjectActuator.execute` performs `floorDiv(multiplyExact(secondTokenBalance, tokenQuant), firstTokenBalance)` with no re-check that `firstTokenBalance != 0` at execute time (only checked in `doValidate`, and a zero divisor here throws `ArithmeticException` = DoS) [6](#0-5) .
- `ExchangeWithdrawActuator.doValidate`/`execute` similarly divides by `firstTokenBalance`/`secondTokenBalance` via `BigInteger`/`BigDecimal` division [7](#0-6) .
- `ExchangeTransactionActuator.doValidate` calls `exchangeCapsule.transaction(...)` again on the corrupted balances and compares against `tokenExpected`; with a corrupted/extreme reserve the computed `anotherTokenQuant` will either revert with "token required must greater than expected" (griefing legitimate traders) or return a wildly mispriced amount that an attacker can exploit for profit [8](#0-7) .

This mirrors the Hyperdrive pattern: an attacker uses a single, permissionless, unprivileged transaction (an `ExchangeTransactionContract`, exactly analogous to "opening the maximum short") to distort a shared AMM reserve so that subsequent legitimate operations either revert (DoS/griefing) or become severely mispriced (fund-extraction opportunity), and there is only a partial/optional (non-default) mitigation (`SafeExchangeProcessor`/`allowHardenExchangeCalculation`), matching DELV's own acknowledgment that they "addressed the issue... which mitigates the part of the issue" but did not eliminate it.

### Impact Explanation
An attacker (any funded account, no special privilege required) can submit an `ExchangeTransactionContract` sized to exhaust one side of a TRX/token Exchange pool's reserve. Because the default calculation path is the legacy floating-point processor with no negative-balance / division-safety guard, this can:
1. Corrupt the persisted `firstTokenBalance`/`secondTokenBalance` state of the exchange (an unbacked/inconsistent reserve, i.e. "unbacked balance" of an on-chain asset pool).
2. Cause subsequent `ExchangeInject`, `ExchangeWithdraw`, and `ExchangeTransaction` calls from any user to either throw `ArithmeticException` (transaction failure/DoS — griefing anyone trying to use the exchange) or compute grossly incorrect swap amounts that the attacker can then exploit to extract more value than deposited, permanently freezing or draining legitimate liquidity providers' funds in that Exchange pool. This satisfies "unauthorized account operation / theft or permanent freezing of funds / unbacked balance."

### Likelihood Explanation
Likelihood is Medium: exploitation requires the attacker to fund a large `sellTokenQuant` trade to skew the pool (bounded by `dynamicStore.getExchangeBalanceLimit()` and the attacker's own asset/TRX holdings), similar to the "large fees" caveat DELV noted for Hyperdrive. It does not require compromising any privileged role (SR/witness/committee) — only a normal `ExchangeTransactionContract` from any account — and the vulnerable (non-hardened) code path is the default unless the `AllowHardenExchangeCalculation` proposal has been activated on the specific chain instance.

### Recommendation
- Make the hardened, non-negative-checked `SafeExchangeProcessor` calculation mandatory (remove the `allowHardenExchangeCalculation` toggle / default it to on) so that `ExchangeCapsule.transaction` always rejects trades that would drive either reserve to zero or negative, regardless of `hardenedCalc`.
- Add explicit reserve-floor/zero checks in `ExchangeInjectActuator.execute` and `ExchangeWithdrawActuator.execute` immediately before dividing, not only in `doValidate`, to avoid TOCTOU-style griefing between validate and execute.
- Consider bounding the maximum fraction of a reserve that a single `ExchangeTransactionContract` can consume in one transaction, similar to slippage/impact limits on other AMMs.

### Proof of Concept
1. Attacker creates or identifies an `Exchange` pool with reserves `(firstTokenBalance, secondTokenBalance)` on a chain where `AllowHardenExchangeCalculation` has not been enabled (default state).
2. Attacker submits an `ExchangeTransactionContract` with `tokenId = firstTokenID` and `quant` chosen so that the legacy `ExchangeProcessor.exchangeFromSupply` (double-precision `Maths.pow`) returns a `buyTokenQuant` close to or exceeding `secondTokenBalance`, driving `newSecondTokenBalance` in `ExchangeCapsule.transaction` down to a tiny, zero, or negative value with `hardenedCalc = false`, so no exception is thrown and the corrupted state is persisted via `Commons.putExchangeCapsule`.
3. A victim then submits a normal `ExchangeInjectContract`/`ExchangeWithdrawContract`/`ExchangeTransactionContract` on the same exchange id; the ratio math (`floorDiv(multiplyExact(secondTokenBalance, tokenQuant), firstTokenBalance)` or BigDecimal division against the corrupted `secondTokenBalance`) either throws `ArithmeticException` (transaction fails, DoS) or returns a severely mispriced `anotherTokenQuant`, which the attacker can exploit in a follow-up trade to extract disproportionate value from the pool.

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

**File:** actuator/src/main/java/org/tron/core/actuator/AbstractExchangeActuator.java (L13-15)
```java
  protected boolean allowHarden() {
    return chainBaseManager.getDynamicPropertiesStore().allowHardenExchangeCalculation();
  }
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L93-96)
```java
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L71-83)
```java
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L74-89)
```java
      BigInteger bigFirstTokenBalance = new BigInteger(String.valueOf(firstTokenBalance));
      BigInteger bigSecondTokenBalance = new BigInteger(String.valueOf(secondTokenBalance));
      BigInteger bigTokenQuant = new BigInteger(String.valueOf(tokenQuant));
      if (Arrays.equals(tokenID, firstTokenID)) {
        anotherTokenID = secondTokenID;
        anotherTokenQuant = bigSecondTokenBalance.multiply(bigTokenQuant)
            .divide(bigFirstTokenBalance).longValueExact();
        exchangeCapsule.setBalance(subtractExact(firstTokenBalance, tokenQuant),
            subtractExact(secondTokenBalance, anotherTokenQuant));
      } else {
        anotherTokenID = firstTokenID;
        anotherTokenQuant = bigFirstTokenBalance.multiply(bigTokenQuant)
            .divide(bigSecondTokenBalance).longValueExact();
        exchangeCapsule.setBalance(subtractExact(firstTokenBalance, anotherTokenQuant),
            subtractExact(secondTokenBalance, tokenQuant));
      }
```
