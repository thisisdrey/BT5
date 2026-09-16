### Title
Non-hardened Exchange trade path allows pool token balances to go to zero/negative, permanently freezing exchange funds - ([File: actuator/src/main/java/org/tron/core/capsule/ExchangeCapsule.java])

### Summary
`ExchangeCapsule.transaction()`, invoked by `ExchangeTransactionActuator.execute()`, only guards against negative resulting token-pool balances when the `hardenedCalc` flag (`allowHarden()`, backed by `DynamicPropertiesStore.getAllowHardenExchangeCalculation`) is enabled. In the default/legacy (non-hardened) path, no post-trade check exists, so a single validated trade can drive `firstTokenBalance` or `secondTokenBalance` to zero or below, permanently bricking the exchange pool — directly analogous to the OUSD `changeSupply` bug where a derived ratio (`rebasingCreditsPerToken`) could be pushed to zero because the code validated the pre-state instead of the resulting invariant.

### Finding Description
`ExchangeTransactionActuator.doValidate()` only checks that the *current* pool balances are non-zero before the trade: [1](#0-0) 

It never re-checks the balances that will result *after* applying the trade. The actual balance mutation happens in `ExchangeCapsule.transaction()`: [2](#0-1) 

The only safety check for resulting balances is gated behind `hardenedCalc`:
```java
if (hardenedCalc && (newFirstTokenBalance < 0 || newSecondTokenBalance < 0)) {
  throw new ContractValidateException("Exchange balance must be >=0 after transaction");
}
```
When `hardenedCalc` is `false` (the legacy, non-hardened `ExchangeProcessor` path, selected whenever `DynamicPropertiesStore.getAllowHardenExchangeCalculation()` is not enabled for the chain), this guard is skipped entirely, and `this.exchange` is unconditionally updated to `newFirstTokenBalance`/`newSecondTokenBalance` even if one of them is zero or negative: [3](#0-2) 

The non-hardened `ExchangeProcessor.exchangeFromSupply()` computes the output quantity using floating-point `Math.pow`, which for extreme/edge-case ratios (e.g., a thin pool combined with a large, but still `balanceLimit`-compliant, `tokenQuant`) can return a `buyTokenQuant` that meets or exceeds `buyTokenBalance`: [4](#0-3) 

This mirrors the reported OUSD pattern exactly: a derived per-unit quantity (there, `rebasingCreditsPerToken`; here, the post-trade token balance/ratio) is validated only against pre-transaction state, so a single crafted transaction can push a core invariant (pool balance must stay > 0) to zero, after which all subsequent legitimate operations on that exchange (`ExchangeTransactionActuator`, `ExchangeInjectActuator`, `ExchangeWithdrawActuator`) revert with "Token balance in exchange is equal with 0, the exchange has been closed", permanently freezing any assets still recorded against that exchange ID and unrecoverable by any account.

### Impact Explanation
This is reachable by any unprivileged account that can broadcast an `ExchangeTransactionContract` (an ordinary order-placer action, explicitly within the "order placer" reachable-actor scope). Successfully driving a pool balance to zero/negative permanently freezes the exchange: creators can no longer withdraw pooled tokens via `ExchangeWithdrawActuator` (blocked by the `firstTokenBalance == 0` guard) and no further trades are possible, resulting in permanent freezing of funds — a concrete High-severity impact category per the validation rules.

### Likelihood Explanation
Reaching this requires precise selection of `tokenQuant` relative to the current (attacker-observable, on-chain) pool balances so that the floating-point-based `ExchangeProcessor` output equals or exceeds the counter-side balance while `newTokenBalance <= balanceLimit` still holds. This is plausible for low-liquidity/newly created exchange pairs (a permissionless, anonymous action via `ExchangeCreateActuator`), making the scenario feasible for an attacker who creates a thin pool and then drains it in one transaction, but it does depend on specific numeric conditions rather than being trivially triggerable for arbitrary pools.

### Recommendation
- **Short term**: In `ExchangeCapsule.transaction()`, always validate that `newFirstTokenBalance > 0` and `newSecondTokenBalance > 0` regardless of `hardenedCalc`, not only when the harden flag is enabled, and reject the transaction (throw `ContractValidateException`) otherwise.
- **Long term**: Make the hardened, `BigDecimal`-based calculation (`SafeExchangeProcessor`) the only supported path, deprecating the floating-point `ExchangeProcessor`, and add invariant/property-based tests (e.g., via Echidna-equivalent fuzzing for Java) asserting pool balances and derived ratios never reach zero as a result of a single validated transaction.

### Proof of Concept
Conceptual sequence (exact numeric parameters would need empirical tuning against `ExchangeProcessor.exchangeFromSupply`'s floating-point formula):
1. Attacker calls `ExchangeCreateActuator` to create a new exchange pair with a small `secondTokenBalance` (e.g., near the minimum allowed).
2. Attacker (or colluding account) calls `ExchangeTransactionActuator` with a `tokenQuant` selling into the pool sized so that `exchangeCapsule.transaction()` (non-hardened path, `dynamicStore.allowStrictMath()`/`allowHarden()` disabled) computes `anotherTokenQuant >= secondTokenBalance`.
3. `doValidate()` passes because `firstTokenBalance`/`secondTokenBalance` were non-zero *before* the trade, and `newTokenBalance` (the *selling* side) stays under `balanceLimit`.
4. `execute()` calls `exchangeCapsule.transaction(...)`, setting `secondTokenBalance` to zero or negative without any rejection, per: [3](#0-2) .
5. All subsequent calls to `ExchangeTransactionActuator`, `ExchangeInjectActuator`, or `ExchangeWithdrawActuator` on this exchange ID now fail validation with "Token balance in exchange is equal with 0, the exchange has been closed", permanently freezing remaining pooled assets.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L194-197)
```java
    if (firstTokenBalance == 0 || secondTokenBalance == 0) {
      throw new ContractValidateException("Token balance in exchange is equal with 0,"
          + "the exchange has been closed");
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

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java (L31-45)
```java
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
