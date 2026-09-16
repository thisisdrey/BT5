### Title
Unprivileged `ExchangeTransactionContract` calls can corrupt shared bancor-style exchange pool state with unchecked negative balances - (File: `chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java`)

### Summary
The Sherlock report describes `BaseAsyncSwapper` being permanently broken by any unprivileged caller because the contract mutates a *shared* piece of state (the aggregator's ERC-20 allowance) without validating the post-condition, so a single malicious call can leave that shared state corrupted for every future legitimate caller. The closest reachable analog in java-tron is the TRC10 `Exchange` (bancor-style) pool: `ExchangeTransactionActuator` lets **any account** submit an `ExchangeTransactionContract` that mutates the shared `ExchangeCapsule` balances, and the "legacy" (non-hardened) code path performs this mutation without validating that the resulting balances stay non-negative — unlike the newer "hardened" path which added that exact check.

### Finding Description
`ExchangeTransactionActuator.execute` calls `exchangeCapsule.transaction(tokenID, tokenQuant, dynamicStore.allowStrictMath(), allowHarden())` [1](#0-0) , which is fully attacker-controlled: `tokenID` and `tokenQuant` come directly from the unprivileged caller's transaction, and the exchange itself is a shared pool that anyone holding a whitelisted-by-nobody TRC10 token can trade against.

Inside `ExchangeCapsule.transaction`, the actual balance update path differs based on `hardenedCalc`:
```
if (hardenedCalc && (newFirstTokenBalance < 0 || newSecondTokenBalance < 0)) {
  throw new ContractValidateException("Exchange balance must be >=0 after transaction");
}
this.exchange = this.exchange.toBuilder()
    .setFirstTokenBalance(newFirstTokenBalance)
    .setSecondTokenBalance(newSecondTokenBalance)
    .build();
``` [2](#0-1) 

The negative-balance guard is only applied when `hardenedCalc` is `true`. `hardenedCalc` is driven by `AbstractExchangeActuator.allowHarden()`, which reads a dynamic (hard-fork) property `allowHardenExchangeCalculation()` [3](#0-2) . When that hard-fork switch is not active (its default/unactivated state), every `ExchangeTransactionContract` is processed through the legacy `ExchangeProcessor`, which computes buy/sell amounts using `double` floating-point math with no bound/overflow/negative checks at all:
```
private long exchangeFromSupply(long balance, long supplyQuant) {
  ...
  double exchangeBalance = balance * (Maths.pow(1.0 + (double) supplyQuant / supply, 2000.0, ...) - 1.0);
  return (long) exchangeBalance;
}
``` [4](#0-3) 

In the legacy path, `ExchangeCapsule.transaction` still writes `newFirstTokenBalance`/`newSecondTokenBalance` unconditionally with no lower-bound check [5](#0-4) . Because `Maths.pow`/double arithmetic is subject to rounding error and does not guarantee `buyTokenQuant <= buyTokenBalance`, a crafted `tokenQuant` from an unprivileged trader can, in principle, cause `buyTokenQuant` to exceed the counterpart balance, driving the persisted balance negative. The only pre-execution guard in `ExchangeTransactionActuator.doValidate` checks `firstTokenBalance == 0 || secondTokenBalance == 0` to detect a "closed" exchange [6](#0-5)  — it does **not** check for negative values, so once a balance goes negative the exchange is not flagged as closed, and all subsequent bancor-formula calculations for that pool operate on corrupted/negative balances shared by every user of that exchange.

This mirrors the report's bug class precisely: an unprivileged, single-transaction caller can mutate global/shared protocol state (the exchange's pool balances) without any validation that the mutation kept the state consistent, permanently degrading the resource for all subsequent unrelated users — just as any user could corrupt `BaseAsyncSwapper`'s shared allowance and break it for all future swap calls.

### Impact Explanation
If the legacy (non-hardened) exchange math path admits negative or otherwise inconsistent pool balances, the TRC10 bancor exchange becomes unusable/incorrect for every subsequent trader against that pool — a shared resource permanently broken by a single unprivileged transaction, and a potential vector to mis-price/drain the pool for other traders (loss of funds for other exchange participants, since the pricing formula on subsequent trades operates on corrupted balances). This satisfies the "unauthorized account operation / permanent freezing or corruption of shared funds-bearing state" bar for validity.

### Likelihood Explanation
The `ExchangeTransactionContract` is a standard, unauthenticated broadcastable transaction type reachable by any account holding minimal balance of either pool token; no special privilege is required, matching the "unprivileged transaction broadcaster" reachable surface. Exploitability depends on whether the double-precision bancor formula (`Maths.pow`) can be driven to overshoot the true balance for a maliciously chosen `sellTokenQuant`/pool ratio — this is plausible given floating point pow computation over the wide `(0.0005, 2000.0)` exponent range used, but I was not able to fully construct and verify a concrete numeric overshoot within the available tool budget, so likelihood should be treated as engineering-plausible rather than empirically confirmed here.

### Recommendation
Apply the same non-negative-balance invariant check performed in the hardened path (`newFirstTokenBalance < 0 || newSecondTokenBalance < 0`) unconditionally, regardless of the `allowHardenExchangeCalculation` hard-fork flag, so that the legacy `ExchangeProcessor` path can never persist a corrupted/negative balance to the shared `ExchangeCapsule`. Additionally, update the "exchange has been closed" validation in `ExchangeTransactionActuator.doValidate` to reject `firstTokenBalance <= 0 || secondTokenBalance <= 0` rather than strict `== 0`.

### Proof of Concept
Not independently reproduced with concrete numeric inputs due to tool-call budget exhaustion before a floating-point overshoot scenario could be derived and validated against `ExchangeProcessor.exchangeFromSupply`/`exchangeToSupply`. The structural root cause (unconditional balance write in the legacy path with no post-condition check, contrasted with the explicit check added only for the hardened path) is confirmed by the cited code at `chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java:124-168` and `actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java:149-224`.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L67-69)
```java
      byte[] anotherTokenID;
      long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
          dynamicStore.allowStrictMath(), allowHarden());
```

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

**File:** actuator/src/main/java/org/tron/core/actuator/AbstractExchangeActuator.java (L13-15)
```java
  protected boolean allowHarden() {
    return chainBaseManager.getDynamicPropertiesStore().allowHardenExchangeCalculation();
  }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java (L31-39)
```java
  private long exchangeFromSupply(long balance, long supplyQuant) {
    supply -= supplyQuant;

    double exchangeBalance = balance
        * (Maths.pow(1.0 + (double) supplyQuant / supply, 2000.0, this.useStrictMath) - 1.0);
    logger.debug("exchangeBalance: " + exchangeBalance);

    return (long) exchangeBalance;
  }
```
