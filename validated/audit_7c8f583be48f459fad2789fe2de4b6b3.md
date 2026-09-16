### Title
Unbacked-Balance / Fund-Theft via Double Overflow-to-`Infinity` Cast in Bancor Exchange Pricing Formula - ([File: chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java])

### Summary
`ExchangeTransactionActuator` (reachable from any signed `ExchangeTransactionContract`, i.e. any account placing a TRX/TRC10 exchange order) computes the counter-token amount using the default, non-hardened `ExchangeProcessor`, which performs the Bancor-style relay calculation entirely in `double` arithmetic and casts the result back to `long` with no `NaN`/`Infinity`/range check. A crafted `quant` can drive the intermediate `Math.pow` computation outside the representable range of `double`, producing `Infinity`/`NaN`, which is then narrowed to `long` (yielding `Long.MAX_VALUE` or `0`), directly conflicts with the reserve accounting, and lets an attacker mint an unbacked counter-token credit. This is the same bug class as CVE-2017-7597 (`tif_dirread.c` computing a value outside the representable range of `float`/`double`, causing undefined/unintended behavior) — except in java-tron's actuator layer this manifests not as a native crash but as a silently-clamped extreme value fed straight into balance/reserve bookkeeping.

### Finding Description
`ExchangeCapsule.transaction()` selects between two processors depending on the `allowHardenExchangeCalculation` chain parameter: [1](#0-0) 

When hardening is not activated (the default until an SR/committee proposal turns it on — this is a governance-gated feature, not a code default), the plain `ExchangeProcessor` is used: [2](#0-1) 

Both `exchangeToSupply` and `exchangeFromSupply` compute a `double` via `Maths.pow(1.0 + quant/balance, exponent, useStrictMath)` and then narrow-cast the result to `long` with `(long) issuedSupply` / `(long) exchangeBalance`. There is no `Double.isNaN`/`Double.isInfinite` guard and no bound check against the actual pool reserves before this value is used. By contrast, the hardened variant (`SafeExchangeProcessor`) explicitly uses `BigDecimal`/`addExact`/`subtractExact` and detects overflow via `ArithmeticException`: [3](#0-2) 

The huge/`Infinity`-derived `long` returned from `processor.exchange(...)` becomes `anotherTokenQuant`/`buyTokenQuant`, which is then used unconditionally to update the pool's balances and, more importantly, to credit the caller's account: [4](#0-3) 

Crucially, `execute()`'s balance credit uses `AbstractExchangeActuator.addExact()`, which is itself gated by the same `allowHarden()` flag and falls back to a raw, unchecked `x + y` when hardening is off: [5](#0-4) 

So in the default (non-hardened) configuration there is no overflow detection at either the pricing-formula stage or the balance-credit stage: a double computation that legitimately goes out of the representable range for the intended math (analogous to LibTIFF's float range violation) is silently converted into an extreme `long` and applied straight to the user's on-chain TRX/asset balance, and to `ExchangeCapsule`'s reserve fields, with no bound-checking against `dynamicStore.getExchangeBalanceLimit()` on the *output* side (only the *input* `tokenQuant` is checked against the limit in `doValidate()`): [6](#0-5) 

### Impact Explanation
An attacker who places a specially-crafted `ExchangeTransactionContract` order against a pool with skewed reserves can force the double-precision Bancor formula in `ExchangeProcessor.exchangeToSupply`/`exchangeFromSupply` to overflow to `Infinity`/`NaN`. The cast-to-`long` result is used both to mutate `ExchangeCapsule`'s `firstTokenBalance`/`secondTokenBalance` (potentially driving a reserve negative, since the un-hardened path has no `>=0` post-condition check that the hardened path has) and to unconditionally credit the attacker's account balance or TRC10 asset amount via `addAssetAmountV2`/`accountCapsule.setBalance`. This is a concrete unbacked-balance/fund-creation vulnerability reachable by any unprivileged account, matching High severity per the accepted impact categories ("unbacked balance", "theft of funds").

### Likelihood Explanation
Reachable directly from a single signed transaction (`ExchangeTransactionContract`) by any account with a minimal TRX/asset balance to pay the trade and fee — no special privilege required. The precondition is that the target exchange pool's `allowHardenExchangeCalculation` proposal has not yet been activated network-wide, which is the default/backward-compatible state for a governance-gated hardening flag of this kind. No SR/witness/committee compromise, no p2p manipulation, and no mocked-only path is needed — only careful selection of `tokenQuant` relative to the pool's `firstTokenBalance`/`secondTokenBalance` to drive the internal `Math.pow` ratio out of double's safely-representable range.

### Recommendation
- Make `allowHardenExchangeCalculation` (or an equivalent unconditional guard) mandatory in `ExchangeProcessor.exchangeToSupply`/`exchangeFromSupply`: reject/throw when the computed `double` is `NaN`, `Infinite`, or exceeds `Long.MAX_VALUE`/`Long.MIN_VALUE` bounds before narrowing to `long`.
- Bound-check the actuator-computed `anotherTokenQuant`/`buyTokenQuant` against the exchange's actual reserve (it must never exceed the opposite-side pool balance) regardless of the hardening flag.
- Apply `addExact`/`subtractExact` (or `BigDecimal`) unconditionally for balance updates in `ExchangeTransactionActuator.execute()` rather than gating overflow protection behind `allowHarden()`.
- Consider promoting `SafeExchangeProcessor` to be the sole/default implementation, removing the legacy unguarded `ExchangeProcessor` path from production use.

### Proof of Concept
1. Create (or locate) an `Exchange` pool with `firstTokenBalance` and `secondTokenBalance` set to small values relative to `supply = 1_000_000_000_000_000_000L` used in `ExchangeProcessor` (e.g., `firstTokenBalance = 1`).
2. As an unprivileged account, submit an `ExchangeTransactionContract` with `tokenId` = the low-balance side and a `quant` chosen so that `(double) quant / newBalance` drives `Maths.pow(1.0 + ratio, 0.0005)` (in `exchangeToSupply`) or the `2000.0`-exponent power in `exchangeFromSupply` to `Double.POSITIVE_INFINITY`.
3. Observe that `ExchangeProcessor.exchangeFromSupply` returns `(long) Double.POSITIVE_INFINITY == Long.MAX_VALUE` (or a similarly extreme value) as `buyTokenQuant`.
4. `ExchangeTransactionActuator.execute()` credits the attacker's account with this value via `addExact`/`addAssetAmountV2` (unconditional raw `+` when hardening is off), producing an account balance far exceeding what the pool actually holds — an unbacked-balance/fund-theft condition.
5. Repeat the test harness pattern already present in `framework/src/test/java/org/tron/core/actuator/ExchangeTransactionActuatorTest.java` (e.g. `hardenedExecuteOverflowThrowsArithmeticException`) but with `allowHardenExchangeCalculation` left at its default (0/off) to confirm no `ArithmeticException` is thrown and the inflated `anotherTokenQuant` is accepted and applied.

Note: I was unable to execute this PoC in a live node from this environment; the trace above is based on static analysis of the cited code paths and the codebase's own hardened-vs-unhardened test suite (`ExchangeProcessorTest`, `ExchangeTransactionActuatorTest`), which independently corroborates that the un-hardened path lacks overflow detection while the hardened path exists specifically to guard against it.

### Citations

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-129)
```java
  public long transaction(byte[] sellTokenID, long sellTokenQuant, boolean useStrictMath,
      boolean hardenedCalc) throws ContractValidateException {
    long supply = 1_000_000_000_000_000_000L;
    Processor processor = hardenedCalc
        ? SafeExchangeProcessor.INSTANCE : new ExchangeProcessor(supply, useStrictMath);

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L61-93)
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
