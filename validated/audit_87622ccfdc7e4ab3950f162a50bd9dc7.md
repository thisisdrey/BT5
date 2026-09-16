## Title
TRC10 AMM pool balance underflow via unguarded legacy `ExchangeProcessor` math in `ExchangeCapsule.transaction()` - (File: chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java)

### Summary
Velocore's loss stemmed from a Balancer-style CPMM pool whose fee/exchange math could be pushed into an underflow, letting an attacker mint outsized value and drain the pool because the non-hardened arithmetic path had no post-condition check on resulting reserves. java-tron's built-in TRC10 AMM (`Exchange`/`ExchangeV2`) has the same structural weakness: its legacy, non-hardened calculation path computes new pool reserves with unchecked arithmetic and a floating-point bancor-style formula, and the invariant that reserves must stay non-negative is enforced **only** when the "hardened" governance flag is enabled.

### Finding Description
`ExchangeCapsule.transaction()` selects between two `Processor` implementations depending on the `hardenedCalc` flag: the legacy `ExchangeProcessor` (double/float bancor-style math) or `SafeExchangeProcessor` (BigDecimal, overflow-checked). Critically, the resulting-balance safety check is gated behind the same flag: [1](#0-0) 

When `hardenedCalc` is `false` (the legacy/default behavior unless the chain committee has turned on `allowHardenExchangeCalculation`), `newFirstTokenBalance`/`newSecondTokenBalance` are computed with plain `long` arithmetic (`firstTokenBalance + sellTokenQuant`, `secondTokenBalance - buyTokenQuant`) and are stored into the pool **without ever verifying they are non-negative or without overflow**: [2](#0-1) 

`allowHarden()` simply reads a dynamic-store flag, and both `subtractExact`/`addExact` in the actuator base class fall back to raw unchecked arithmetic when that flag is off: [3](#0-2) 

The buy quantity itself is computed by `ExchangeProcessor`, a bancor-style formula using `double` precision (`Math.pow`), which is known to be susceptible to accuracy/rounding errors especially near extreme reserve ratios or large trade sizes: [4](#0-3) 

This whole computation is directly reachable by any unprivileged, signed `ExchangeTransactionContract` — no special permission is required beyond owning the tokens being sold — via `ExchangeTransactionActuator.execute()`, which persists the mutated `ExchangeCapsule` reserves back to the store after calling `transaction()`: [5](#0-4) 

Because `validate()` calls the very same non-guarded `transaction()` to compute the expected output and only rejects if `anotherTokenQuant < tokenExpected` — it does not reject on a negative resultant reserve when hardened math is off: [6](#0-5) 

An attacker who crafts sell quantities that drive the floating-point bancor formula's output past what the opposing reserve actually holds can force `secondTokenBalance - buyTokenQuant` (or the symmetric case) to go negative. Since no hardened-mode invariant check runs, this negative reserve is persisted into `ExchangeStore`/`ExchangeV2Store`. Once a reserve is negative, subsequent trades against the same pool operate on corrupted state — this is directly analogous to Velocore's underflow that let a single crafted withdrawal mint an "egregiously large amount of liquidity tokens," enabling the attacker to drain far more value than the pool legitimately contains. Repo tests explicitly acknowledge this class of issue ("Accuracy problem" test comments, and the newly added `SafeExchangeProcessor`/hardened-mode tests), confirming the legacy path is a known-risky computation that remains live by default.

### Impact Explanation
If reachable, this allows an attacker with ordinary signed transactions to corrupt TRC10 exchange pool reserves into an invalid (negative) state and to extract more tokens/TRX than the pool holds, i.e., unauthorized draining of another user's/pool's funds — a direct funds-theft impact comparable to the $6.8M Velocore drain, scoped to whatever TRX/TRC10 liquidity sits in java-tron's native Exchange/Bancor pools.

### Likelihood Explanation
Exploitability depends on the `allowHardenExchangeCalculation` dynamic parameter remaining disabled (its historical/legacy default) and on finding sell/buy quantities and reserve ratios where the double-precision bancor formula's rounding pushes the computed `buyTokenQuant` past the opposing reserve. This requires careful parameter search but no privileged access — any account holding tokens can submit `ExchangeTransactionContract` transactions to probe and eventually trigger the underflow, making it a Medium-to-High likelihood issue on networks that have not activated the hardened flag.

### Recommendation
Enforce the non-negative reserve invariant unconditionally in `ExchangeCapsule.transaction()`, regardless of `hardenedCalc`, and reject the transaction/throw `ContractValidateException` before persisting corrupted reserves. Longer term, retire the floating-point `ExchangeProcessor` path entirely in favor of `SafeExchangeProcessor` (BigDecimal-based) by making `allowHardenExchangeCalculation` mandatory/always-on rather than an opt-in governance toggle.

### Proof of Concept
1. Ensure `allowHardenExchangeCalculation` is `0` (default/unactivated) so `ExchangeTransactionActuator` uses the legacy path shown at `ExchangeCapsule.java:124-169`.
2. Create (or locate) a TRC10/TRX `Exchange` pool with skewed reserves (large `firstTokenBalance`, small `secondTokenBalance`) via `ExchangeCreateContract`.
3. Submit a crafted `ExchangeTransactionContract` (via `ExchangeTransactionActuator`) selling a quantity engineered so that `ExchangeProcessor.exchange()`'s double-precision bancor formula computes a `buyTokenQuant` exceeding the actual opposing reserve.
4. Because `hardenedCalc` is `false`, no check in `transaction()` rejects the resulting negative `newSecondTokenBalance`; `ExchangeTransactionActuator.execute()` persists the corrupted `ExchangeCapsule` and simultaneously credits the attacker's account with tokens/TRX exceeding the pool's real balance (`accountCapsule.addAssetAmountV2`/`setBalance` at `ExchangeTransactionActuator.java:86-91`).
5. Repeat/drain remaining reserves from the now-corrupted pool.

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L61-96)
```java
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
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L217-221)
```java
    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
    }
```
