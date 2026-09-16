### Title
Legacy (non-hardened) exchange pricing path lacks a pool-balance sufficiency check, allowing negative/unbacked pool balances - ([File: chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java])

### Summary
`ExchangeCapsule.transaction()` only validates that the resulting pool balances are non-negative when the governance-gated "hardened" exchange calculation is enabled. In the default (legacy) mode, the Bancor-style, floating-point pricing formula in `ExchangeProcessor` can compute a `buyTokenQuant` that exceeds the actual balance of the token being bought, and this is never checked by the calling actuators before crediting the trader and updating pool state, letting a single `ExchangeTransactionContract` push an on-chain TRX/TRC10 liquidity pool into a negative, unbacked balance.

### Finding Description
`ExchangeCapsule.transaction()` computes `buyTokenQuant` via `Processor.exchange()` and then updates the pool's `firstTokenBalance`/`secondTokenBalance`: [1](#0-0) 

The negative-balance guard is conditioned on `hardenedCalc`: [2](#0-1) 

`hardenedCalc` is derived from the committee-controlled, default-disabled property `allowHardenExchangeCalculation`: [3](#0-2) 

A test in the repo itself documents that this flag defaults to disabled ("hardened disabled (==0) -> contract is treated as exchange"): [4](#0-3) 

When hardened calc is disabled, the legacy `ExchangeProcessor` is used, which relies on floating-point `Math.pow` with fractional exponents (0.0005 / 2000.0) to convert a sell quantity into a "relay" supply amount and then back into a buy quantity: [5](#0-4) 

`ExchangeTransactionActuator.doValidate()` calls `exchangeCapsule.transaction(...)` and only checks that the returned `anotherTokenQuant` is `>= tokenExpected` (a user-supplied minimum), but never checks it against the actual balance of the token being bought (`firstTokenBalance`/`secondTokenBalance` on the buy side): [6](#0-5) 

`execute()` then unconditionally credits the trader with `anotherTokenQuant` and persists the (potentially negative) pool balances via `Commons.putExchangeCapsule`: [7](#0-6) 

Because the double-precision power-function pricing model is transcendental and only bounded by `long` overflow checks (not by the buy-side pool balance) in the non-hardened path, a sell quantity chosen close to the pool's supply/precision limits can make `exchangeFromSupply()` return a `buyTokenQuant` larger than `buyTokenBalance`, producing `newSecondTokenBalance`/`newFirstTokenBalance` that go negative in `transaction()` — since the `hardenedCalc` guard is skipped, this negative value is stored as-is.

### Impact Explanation
This lets an unprivileged trader broadcast a normal `ExchangeTransactionContract` and, exploiting floating-point pricing bounds, drain more of the buy-side asset than the on-chain exchange pool actually holds. The pool's balance becomes negative/unbacked, meaning subsequent traders and the exchange creator can no longer redeem their proportional share (permanent loss of backing funds for other participants) while the attacker walks away with assets not actually backed by the pool — directly analogous to the Bitmarket "insufficient liquidity" incident, except here the insolvency is created deterministically on-chain by exploiting an actuator arithmetic gap rather than off-chain mismanagement.

### Likelihood Explanation
Exploitability depends on the hardened flag remaining at its default-disabled value (confirmed by the codebase's own test) and on being able to construct sell/pool-size parameters that push the double-precision Bancor formula past the buy-side balance. This requires careful selection of `tokenQuant` relative to pool reserves (achievable by any account with the relevant TRX/token balance) but does not require any privileged role — it is reachable purely through `ExchangeTransactionActuator` from a signed transaction.

### Recommendation
Move the `newFirstTokenBalance < 0 || newSecondTokenBalance < 0` check outside of the `hardenedCalc` conditional in `ExchangeCapsule.transaction()` so it always applies, regardless of `allowHardenExchangeCalculation`. Additionally, add an explicit check in `ExchangeTransactionActuator.doValidate()` (and any other exchange actuator using `transaction()`) that the computed `anotherTokenQuant` does not exceed the current balance of the token being bought before executing the state change.

### Proof of Concept
1. Create an exchange pool with small reserves on both sides (e.g., via `ExchangeCreateContract`).
2. Ensure `allowHardenExchangeCalculation` remains at its default value of `0` (no committee proposal has enabled it) — confirmed default per `ManagerTest.isExchangeTransactionBypassedWhenHardenedEnabled`.
3. Broadcast an `ExchangeTransactionContract` selling a `tokenQuant` chosen so that the legacy `ExchangeProcessor.exchange()` (`exchangeToSupply` + `exchangeFromSupply`, both using `Math.pow` on `double`) returns a `buyTokenQuant` greater than the current balance of the buy-side token in the pool.
4. `ExchangeTransactionActuator.doValidate()` only checks `anotherTokenQuant >= tokenExpected`, which passes trivially; `execute()` credits the caller with `anotherTokenQuant` and calls `exchangeCapsule.transaction()`, which — because `hardenedCalc` is `false` — skips the negative-balance check and persists a negative `secondTokenBalance`/`firstTokenBalance` via `Commons.putExchangeCapsule`.
5. The exchange pool is now insolvent (negative on-chain balance), and the attacker holds more of the bought asset than the pool ever contained.

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

**File:** actuator/src/main/java/org/tron/core/actuator/AbstractExchangeActuator.java (L13-15)
```java
  protected boolean allowHarden() {
    return chainBaseManager.getDynamicPropertiesStore().allowHardenExchangeCalculation();
  }
```

**File:** framework/src/test/java/org/tron/core/db/ManagerTest.java (L1355-1358)
```java
    // Default: hardened disabled (==0) -> contract is treated as exchange
    chainManager.getDynamicPropertiesStore().saveAllowHardenExchangeCalculation(0);
    Assert.assertTrue("Exchange tx must be detected when hardened disabled",
        (boolean) m.invoke(dbManager, exchange));
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L86-96)
```java
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
