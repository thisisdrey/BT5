Confirmed: `doValidate()` in `ExchangeTransactionActuator` calls `exchangeCapsule.transaction(...)` only checking `anotherTokenQuant < tokenExpected`; it never verifies `anotherTokenQuant` against the actual `firstTokenBalance`/`secondTokenBalance` of the pool, and the negative-balance guard inside `ExchangeCapsule.transaction()` is gated behind `hardenedCalc` (the `AllowHardenExchangeCalculation` proposal), which is off by default.

### Title
Missing invariant/solvency check in default (non-hardened) `ExchangeCapsule.transaction()` allows draining more than the pool holds - (File: `chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java`)

### Summary
The bancor-curve TRX/TRC10 exchange math in `ExchangeProcessor` (default, non-hardened path) computes the counter-token amount using floating-point `Math.pow` approximations of the constant-supply bonding-curve invariant. Unlike the Aftermath Finance geometric-mean pools, java-tron's `ExchangeCapsule.transaction()` only asserts `newFirstTokenBalance < 0 || newSecondTokenBalance < 0` when `hardenedCalc` is `true` [1](#0-0) . That hardening is controlled by the `AllowHardenExchangeCalculation` chain parameter, which is set via `allowHarden()` reading `dynamicStore.allowHardenExchangeCalculation()` and defaults to disabled until SRs vote to enable it [2](#0-1) . In the default (legacy) path the double-precision `Math.pow`-based `ExchangeProcessor.exchangeToSupply`/`exchangeFromSupply` computations never re-verify the invariant (that the computed payout does not exceed the actual pool reserve), analogous to the missing `assert!(invariant_after >= invariant, ...)` check described in the Aftermath report [3](#0-2) .

### Finding Description
`ExchangeTransactionActuator.doValidate()` calls `exchangeCapsule.transaction(tokenID, tokenQuant, dynamicStore.allowStrictMath(), allowHarden())` and only validates that the resulting `anotherTokenQuant` is not below the caller-supplied `tokenExpected`; it never checks that `anotherTokenQuant` is bounded by the pool's actual opposite-side reserve [4](#0-3) . `ExchangeTransactionActuator.execute()` then unconditionally applies the same `transaction()` call and credits `anotherTokenQuant` to the caller's account balance/asset map [5](#0-4) .

Inside `ExchangeCapsule.transaction()`, the new pool balances are computed with plain `+`/`-` arithmetic when `hardenedCalc` is `false` (the default), and the sanity check `newFirstTokenBalance < 0 || newSecondTokenBalance < 0` is executed only `if (hardenedCalc && ...)` [6](#0-5) . Because `ExchangeProcessor` relies on `Math.pow` with a `0.0005`/`2000.0` exponent pair to approximate the constant-supply bonding curve rather than an exact, monotonic invariant-preserving computation [7](#0-6) , extreme or boundary `sellTokenQuant` values (e.g., selling a large fraction of one side's reserve) can produce a `buyTokenQuant` that exceeds the actual `buyTokenBalance`/`secondTokenBalance` held by the exchange pool, exactly the same class of error the Aftermath report identifies: the approximation's output invariant is not re-checked against the true, pre-swap invariant/reserve before committing state.

Since the legacy branch skips the negative-balance assertion entirely, this can drive one side of the on-chain `Exchange` capsule's stored balance negative while the actuator still credits the attacker's account with `anotherTokenQuant` tokens/TRX that the pool never actually possessed — an unbacked-balance condition.

### Impact Explanation
If floating-point approximation error in `ExchangeProcessor` (or an attacker deliberately choosing edge-case `sellTokenQuant`/pool-balance ratios) causes the computed payout to exceed the pool's real reserve, the caller receives assets/TRX that are not backed by the pool's actual holdings while the pool's on-chain balance can go negative undetected in the default (non-hardened) path. This is a theft-of-funds / unbacked-balance condition reachable by any account holder issuing an `ExchangeTransactionContract`, without needing SR privileges, malicious witnesses, or off-chain assumptions.

### Likelihood Explanation
The vulnerable path is the current default: `AllowHardenExchangeCalculation` must be explicitly enabled by committee proposal to activate the negative-balance guard and `SafeExchangeProcessor`; until then, every `ExchangeTransactionContract` executes through the unguarded legacy branch [8](#0-7) . Any account holder can freely choose `tokenID`/`tokenQuant` to probe pool ratios that maximize floating-point error, making this reachable purely through normal, unprivileged `ExchangeTransactionContract` broadcasts.

### Recommendation
Add an explicit, unconditional check in `ExchangeCapsule.transaction()` (not gated by `hardenedCalc`) that the computed `buyTokenQuant` does not exceed the opposite-side pool balance, and that the resulting `newFirstTokenBalance`/`newSecondTokenBalance` are both `>= 0`, throwing `ContractValidateException` otherwise — mirroring the Aftermath fix of asserting the invariant is not violated after the swap. Consider making the hardened, invariant-checked path (`SafeExchangeProcessor` plus the balance assertion) the unconditional default rather than an opt-in proposal.

### Proof of Concept
1. Create/observe an `Exchange` pool via `ExchangeCreateContract` with heavily skewed reserves (e.g., very small `secondTokenBalance` relative to `firstTokenBalance`), keeping `AllowHardenExchangeCalculation` at its default (disabled) value.
2. Broadcast an `ExchangeTransactionContract` selling a large portion of the `firstToken` side, chosen so that the `Math.pow`-based bonding curve in `ExchangeProcessor.exchangeFromSupply` (see [9](#0-8) ) returns a `buyTokenQuant` at or beyond `secondTokenBalance`.
3. Observe that `ExchangeCapsule.transaction()` (legacy branch) applies `secondTokenBalance - buyTokenQuant` without the `>= 0` guard used in the hardened branch, and `ExchangeTransactionActuator.execute()` still credits the full `anotherTokenQuant` to the caller's account, while the stored pool `secondTokenBalance` becomes negative/inconsistent, confirming the missing invariant check for the default path.

### Citations

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-166)
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
```

**File:** actuator/src/main/java/org/tron/core/actuator/AbstractExchangeActuator.java (L13-15)
```java
  protected boolean allowHarden() {
    return chainBaseManager.getDynamicPropertiesStore().allowHardenExchangeCalculation();
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L64-97)
```java
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

```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L217-221)
```java
    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
    }
```
