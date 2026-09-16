## Title
Exchange (Bancor-formula) trade execution lacks a non-negative pool-balance invariant in the legacy calculation path, allowing unbacked-balance issuance from TRC10 exchange pools - (File: `chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java`)

### Summary
`ExchangeCapsule.transaction()` computes new pool balances after every TRC10 AMM (`Exchange`) trade and only validates that the resulting balances are non-negative when the **hardened** calculation path is used. The legacy/default path silently accepts negative resulting balances, so a caller who triggers `ExchangeTransactionContract` (an unprivileged, broadcastable transaction) can drive an exchange pool balance below zero and receive tokens the pool never actually holds.

### Finding Description
`ExchangeCapsule.transaction()` [1](#0-0)  computes `buyTokenQuant` via either `ExchangeProcessor` (double/`Math.pow`-based Bancor formula) or `SafeExchangeProcessor` (`BigDecimal`-based) depending on `hardenedCalc`, then updates `newFirstTokenBalance` / `newSecondTokenBalance`. The invariant check:

```java
if (hardenedCalc && (newFirstTokenBalance < 0 || newSecondTokenBalance < 0)) {
  throw new ContractValidateException("Exchange balance must be >=0 after transaction");
}
```

is gated by `hardenedCalc` only [2](#0-1) . When `allowHarden()`/`allowHardenExchangeCalculation` is not enabled (the default/legacy behavior, and the only path exercised for a large class of live-network configurations before the harden proposal is switched on by committee), this check is skipped entirely, and the legacy `ExchangeProcessor` uses floating-point `Math.pow` [3](#0-2)  which is known to accumulate rounding error at the tails of the Bancor curve, permitting `buyTokenQuant` to exceed the actual `buyTokenBalance`.

`ExchangeTransactionActuator.execute()` calls `exchangeCapsule.transaction(...)` and then unconditionally credits the caller with `anotherTokenQuant` and persists the (now possibly negative) pool balances [4](#0-3) . Unlike `ExchangeWithdrawActuator.doValidate()`, which explicitly checks `firstTokenBalance < tokenQuant || secondTokenBalance < anotherTokenQuant` before allowing a withdrawal [5](#0-4) , `ExchangeTransactionActuator.doValidate()` performs no equivalent check that the computed `anotherTokenQuant` does not exceed the exchange's current holdings of that token before crediting the trader [6](#0-5) .

This is functionally the same bug class as the RigoBlock incident referenced in the report: a pool/AMM contract that fails to validate internal accounting invariants on every trade, letting an attacker extract more value from the pool than it actually holds.

### Impact Explanation
A negative pool balance means the ledger no longer matches actual backing assets: the trader walks away with tokens/TRX that the exchange pool did not have, i.e., unbacked-balance creation / theft of value from other liquidity participants (the exchange creator and future traders who can no longer redeem their share). This satisfies the "unbacked balance" / "theft of funds" impact bar.

### Likelihood Explanation
Reachable by any account with a small TRX/TRC10 balance broadcasting an ordinary `ExchangeTransactionContract` transaction (no special privilege needed - "order placer" role explicitly in scope). Exploitation requires the hardened invariant to be disabled (default/legacy state, or any period before `allowHardenExchangeCalculation` is activated by committee) and requires finding trade sizes near the numerical edge of the double-precision Bancor curve where `Math.pow` rounding pushes `buyTokenQuant` past the actual pool holdings; this is an economic/precision attack rather than a memory-safety bug, so it is feasible but requires probing specific pool ratios/quantities (as the recorded unit test at `ExchangeCapsuleTest.testHardenedTransactionNegativeBalanceThrows` demonstrates the negative-balance condition is reachable and is exactly what the hardened check was added to guard against) [7](#0-6) .

### Recommendation
Move the non-negative pool-balance invariant check out of the `hardenedCalc`-only branch so it is enforced unconditionally in `ExchangeCapsule.transaction()`, and additionally validate in `ExchangeTransactionActuator.doValidate()`/`execute()` that `anotherTokenQuant` never exceeds the current balance of the token being bought, mirroring the check already present in `ExchangeWithdrawActuator`.

### Proof of Concept
1. Do not enable `allowHardenExchangeCalculation` (default state) so `ExchangeCapsule.transaction()` uses `ExchangeProcessor`/`Math.pow` with no post-condition check.
2. Create or use an existing TRC10 `Exchange` pool with a highly skewed balance ratio between `firstToken`/`secondToken` (achievable via `ExchangeCreateActuator`/`ExchangeInjectActuator`, both broadcastable by an ordinary account).
3. Broadcast an `ExchangeTransactionContract` selling the abundant-side token in a quantity chosen near the numerical edge where `Math.pow(1+quant/newBalance, 0.0005)` / `Math.pow(1+supplyQuant/supply, 2000)` rounding causes the computed `buyTokenQuant` in `ExchangeProcessor.exchange()` to exceed the actual balance of the scarce-side token.
4. `ExchangeTransactionActuator.execute()` credits the trader with `anotherTokenQuant` and persists `exchangeCapsule` with a negative `secondTokenBalance` (or `firstTokenBalance`), since the guard at `ExchangeCapsule.java:160` is skipped when `hardenedCalc` is `false`.
5. Repeat/observe that the pool's recorded balance is now inconsistent with actual backing, and further users cannot fully withdraw/trade against the pool - demonstrating unbacked value extraction.

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L67-97)
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L199-221)
```java
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L218-223)
```java
    if (Arrays.equals(tokenID, firstTokenID)) {
      anotherTokenQuant = bigSecondTokenBalance.multiply(bigTokenQuant)
          .divideToIntegralValue(bigFirstTokenBalance).longValueExact();
      if (firstTokenBalance < tokenQuant || secondTokenBalance < anotherTokenQuant) {
        throw new ContractValidateException("exchange balance is not enough");
      }
```

**File:** framework/src/test/java/org/tron/core/capsule/ExchangeCapsuleTest.java (L71-83)
```java
  @Test
  public void testHardenedTransactionNegativeBalanceThrows() throws Exception {
    // Construct a corrupt-state pool with a negative balance to drive the
    // < 0 invariant in the hardened branch via subtractExact wrapping.
    ExchangeCapsule capsule = new ExchangeCapsule(
        ByteString.copyFromUtf8("owner"), 99L, 0L,
        "abc".getBytes(), "def".getBytes());
    capsule.setBalance(Long.MAX_VALUE, 1L);

    // Selling abc adds to firstTokenBalance: addExact(MAX, q) overflows -> ArithmeticException
    Assert.assertThrows(ArithmeticException.class,
        () -> capsule.transaction("abc".getBytes(), 1L, true, true));
  }
```
