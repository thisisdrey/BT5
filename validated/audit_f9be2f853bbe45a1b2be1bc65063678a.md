### Title
Unchecked exchange balance arithmetic in `ExchangeCapsule.transaction()` (non-hardened mode) can drive TRC10/TRX exchange pool balances negative - ([File: chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java])

### Summary
The pattern reported in the external finding — chained `add`/`sub`/`mul`/`div` operations on pool balances/invariants without bounds validation before the result is persisted — has a direct analog in java-tron's bancor-style TRC10 exchange (`ExchangeTransactionContract`/`ExchangeInjectContract`/`ExchangeWithdrawContract`). `ExchangeCapsule.transaction()` computes `buyTokenQuant` via floating-point Bancor curve math and then updates `newFirstTokenBalance`/`newSecondTokenBalance` with plain `+`/`-` unless the `allowHardenExchangeCalculation` (dynamic parameter) is enabled, in which case only that hardened branch validates the post-condition.

### Finding Description
`ExchangeCapsule.transaction()` [1](#0-0)  computes `buyTokenQuant` from `ExchangeProcessor.exchange()` (double-precision Bancor formula) [2](#0-1)  and then updates the pool balances:

```java
newFirstTokenBalance = hardenedCalc ? StrictMathWrapper.addExact(...) : firstTokenBalance + sellTokenQuant;
newSecondTokenBalance = hardenedCalc ? StrictMathWrapper.subtractExact(...) : secondTokenBalance - buyTokenQuant;
...
if (hardenedCalc && (newFirstTokenBalance < 0 || newSecondTokenBalance < 0)) {
  throw new ContractValidateException(...);
}
``` [3](#0-2) 

The post-transaction non-negativity check is only performed when `hardenedCalc` (i.e., `allowHardenExchangeCalculation()`) is true. This flag is a chain parameter that must be explicitly enabled via governance/proposal [4](#0-3) ; unless activated, the exchange path falls back to unchecked plain `+`/`-` on `long` pool balances with no post-update validation that the resulting balance is non-negative or that `buyTokenQuant` does not exceed the buy-side balance.

`ExchangeTransactionActuator.doValidate()` only checks that the *sold* token's resulting balance stays under `balanceLimit` and that the *received* quantity meets `tokenExpected`; it never checks that `anotherTokenQuant` (the computed buy amount) is bounded by the corresponding pool's actual reserve [5](#0-4) . Because the Bancor curve computation uses `double` arithmetic (`Math.pow`), floating-point rounding on extreme/adversarially chosen `quant` values (e.g., very small or very large relative to balance) can produce a `buyTokenQuant` that, when subtracted from `secondTokenBalance`, yields a negative `long` balance that is silently accepted and persisted (no `ArithmeticException`, no `ContractValidateException`) in the non-hardened path.

### Impact Explanation
A negative or corrupted pool balance for one side of a TRC10/TRX exchange pair is a form of unbacked-balance / accounting corruption: subsequent `ExchangeTransactionActuator`/`ExchangeWithdrawActuator` operations against that exchange would compute further trades or withdrawals off a corrupted reserve figure, potentially allowing extraction of tokens from the pool beyond what was actually deposited, or permanently bricking the exchange (since `firstTokenBalance == 0 || secondTokenBalance == 0` checks treat the exchange as closed, but a negative value bypasses this equality check and continues to be used in subsequent Bancor formula divisions, producing further undefined output).

### Likelihood Explanation
This requires `allowHardenExchangeCalculation` to remain disabled (i.e., the hardening proposal has not been activated on the target network) and precise selection of trade quantities to induce floating-point edge cases in the Bancor curve — a data-dependent, moderately difficult but realistic condition for an unprivileged trader submitting `ExchangeTransactionContract` transactions, since no special privilege is needed to create/trade on an exchange pool.

### Recommendation
Make the non-negative post-condition check (`newFirstTokenBalance < 0 || newSecondTokenBalance < 0`) unconditional (not gated behind `hardenedCalc`), and additionally validate in `ExchangeTransactionActuator.doValidate()`/`execute()` that the computed `anotherTokenQuant` never exceeds the corresponding pool's current balance, independent of whether the harden flag is active.

### Proof of Concept
Not independently reproduced with a concrete numeric example — the floating-point rounding conditions required to force `buyTokenQuant > buyTokenBalance` in the Bancor formula were not verified experimentally within this analysis; this is flagged as an analog based on the unconditional-arithmetic pattern matching the reported bug class, not as a confirmed exploit.

### Citations

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

**File:** actuator/src/main/java/org/tron/core/actuator/AbstractExchangeActuator.java (L13-15)
```java
  protected boolean allowHarden() {
    return chainBaseManager.getDynamicPropertiesStore().allowHardenExchangeCalculation();
  }
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
