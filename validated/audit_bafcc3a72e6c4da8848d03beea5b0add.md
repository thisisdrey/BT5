### Title
Unchecked pool-balance arithmetic in `ExchangeCapsule.transaction` allows unbacked/negative TRX-token exchange balances - (File: `chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java`)

### Summary
The Bancor-style TRX↔token swap pool (`Exchange`/`ExchangeV2`) updates its `firstTokenBalance`/`secondTokenBalance` using plain Java `+`/`-` arithmetic by default, and the negative-balance invariant check that would catch an underflow is only executed when an opt-in "hardened" calculation mode is active. This mirrors the reported Superposition bug class: safety checks/checked arithmetic exist but are not applied on the path that actually executes by default, so the amount already swapped in/out of the pool can silently underflow.

### Finding Description
`ExchangeCapsule.transaction()` computes new pool balances after a swap: [1](#0-0) 

When `hardenedCalc` is `false` (the legacy/default path selected whenever `allowHarden()` returns `false`), the balances are updated with unchecked native `+`/`-`:
```
newFirstTokenBalance = hardenedCalc ? ... : firstTokenBalance + sellTokenQuant;
newSecondTokenBalance = hardenedCalc ? ... : secondTokenBalance - buyTokenQuant;
```
and the resulting invariant check is gated on the same flag: [2](#0-1) 
```
if (hardenedCalc && (newFirstTokenBalance < 0 || newSecondTokenBalance < 0)) {
  throw new ContractValidateException("Exchange balance must be >=0 after transaction");
}
```
So in default (non-hardened) mode, if `buyTokenQuant` (computed by the Bancor-formula `ExchangeProcessor`) ever exceeds the current pool balance of the token being bought — which can happen at extreme ratios, boundary/rounding conditions of the price-curve math, or via repeated small trades that accumulate rounding drift — `secondTokenBalance - buyTokenQuant` silently produces a negative `long` and is persisted to the store with no exception raised, because Java's primitive long subtraction wraps/underflows silently exactly like the Rust release-mode behavior described in the report.

Whether this path is reachable depends on `AbstractExchangeActuator.allowHarden()`: [3](#0-2) 
which is controlled by the `allowHardenExchangeCalculation` dynamic property, itself only settable via a committee proposal (`ALLOW_HARDEN_EXCHANGE_CALCULATION` in `ProposalUtil`/`ProposalService`). Any unprivileged account can invoke `ExchangeTransactionActuator`/`ExchangeInjectActuator`/`ExchangeWithdrawActuator` against any live `Exchange` pool (including a pool the attacker created via the also-unprivileged `ExchangeCreateActuator`) via a signed `ExchangeTransactionContract`, so the vulnerable arithmetic is reachable from a single normal transaction as long as the hardened mode has not been activated by committee vote on the target network. [4](#0-3) 

### Impact Explanation
A negative or wrapped pool balance corrupts the AMM invariant used by every subsequent `ExchangeInjectActuator`/`ExchangeWithdrawActuator`/`ExchangeTransactionActuator` call against that pool (their Bancor-ratio math, e.g. `secondTokenBalance.multiply(tokenQuant).divide(firstTokenBalance)`, assumes non-negative balances). This can let an attacker drain more tokens than the pool actually holds (unbacked balance / theft of funds) or permanently corrupt/freeze the exchange pool's accounting, since there is no recovery path once a negative balance is committed to the `ExchangeV2Store`.

### Likelihood Explanation
The unchecked path is the *default* behavior for every network that has not passed the `ALLOW_HARDEN_EXCHANGE_CALCULATION` committee proposal — the repository's own tests explicitly acknowledge this default/legacy vs. "hardened" split (`SameTokenNameCloseTokenBalanceZero`, `hardenedExecuteOverflowThrowsArithmeticException`, `testHardenedTransactionNegativeBalanceThrows`), confirming maintainers are aware unchecked arithmetic exists but chose an opt-in mitigation rather than a default fix. Exploitation requires no special privileges: create an exchange pool and craft trade sizes/ratios that push `buyTokenQuant` above the current opposing balance under the legacy Bancor math.

### Recommendation
Remove the `hardenedCalc` gate and always use checked arithmetic (`StrictMathWrapper.addExact`/`subtractExact`) plus the `newFirstTokenBalance < 0 || newSecondTokenBalance < 0` invariant check in `ExchangeCapsule.transaction`, matching the "checked_add/checked_sub" mitigation recommended in the original report, instead of leaving it dependent on a not-yet-activated proposal flag.

### Proof of Concept
1. Unprivileged account calls `ExchangeCreateActuator` to create a TRX/TOKEN pool with attacker-chosen `firstTokenBalance`/`secondTokenBalance`.
2. With `allowHardenExchangeCalculation` at its default (inactive) value, repeatedly call `ExchangeTransactionActuator` (`ExchangeTransactionContract`) selling into the pool at ratios/quantities that drive the Bancor-computed `buyTokenQuant` above the current opposing token balance.
3. `ExchangeCapsule.transaction()` executes `secondTokenBalance - buyTokenQuant` unchecked (lines 124-158), producing a negative `long` balance that is stored via `Commons.putExchangeCapsule` with no `ContractValidateException` thrown, since the `hardenedCalc` guard at lines 160-162 is skipped.
4. Subsequent trades against the corrupted pool use the negative balance in ratio math, enabling extraction of more tokens than the pool holds.

### Citations

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-158)
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
```

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L160-162)
```java
    if (hardenedCalc && (newFirstTokenBalance < 0 || newSecondTokenBalance < 0)) {
      throw new ContractValidateException("Exchange balance must be >=0 after transaction");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/AbstractExchangeActuator.java (L13-23)
```java
  protected boolean allowHarden() {
    return chainBaseManager.getDynamicPropertiesStore().allowHardenExchangeCalculation();
  }

  public long subtractExact(long x, long y) {
    return allowHarden() ? StrictMathWrapper.subtractExact(x, y) : x - y;
  }

  public long addExact(long x, long y) {
    return allowHarden() ? StrictMathWrapper.addExact(x, y) : x + y;
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L57-69)
```java
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
```
