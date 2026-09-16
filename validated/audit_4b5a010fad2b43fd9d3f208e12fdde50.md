### Title
Unchecked exchange-quote invariant in legacy path allows unvalidated pool state to be persisted - ([File: chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java])

### Summary
`ExchangeCapsule.transaction()` computes a swap quote via a `Processor` (either the double/`Maths.pow`-based legacy `ExchangeProcessor` or the BigDecimal-based `SafeExchangeProcessor`) and then updates the pool's reserve balances. The result is validated against the `>= 0` invariant only when the hardened path is used; in the default/legacy path the computed reserve balances are written to the exchange store and the counter-token amount is credited to the trader's account without any check that the quote is valid, exactly mirroring the unchecked `getBuyNFTQuote()`/`getSellNFTQuote()` pattern from the report.

### Finding Description
`ExchangeCapsule.transaction(byte[] sellTokenID, long sellTokenQuant, boolean useStrictMath, boolean hardenedCalc)` selects a `Processor` implementation and computes `buyTokenQuant`, `newFirstTokenBalance`, `newSecondTokenBalance`: [1](#0-0) 

The sanity check that the resulting reserves cannot go negative is gated behind `hardenedCalc`:
```
if (hardenedCalc && (newFirstTokenBalance < 0 || newSecondTokenBalance < 0)) {
  throw new ContractValidateException(...);
}
```
When `hardenedCalc` is `false` (the legacy `ExchangeProcessor`, driven by floating-point `Maths.pow` in `exchangeToSupply`/`exchangeFromSupply`), this invariant is never checked, and the new (potentially inconsistent) `firstTokenBalance`/`secondTokenBalance` are unconditionally written into `this.exchange` and then persisted: [2](#0-1) 

`ExchangeTransactionActuator.execute()` — reachable from a single signed `ExchangeTransactionContract` broadcast by any account — calls `exchangeCapsule.transaction(...)` and directly uses the returned `anotherTokenQuant` to credit the trader's balance/asset and to persist the mutated `ExchangeCapsule` to the store, with no post-hoc validation of the quote or resulting reserves: [3](#0-2) 

Whether `hardenedCalc` is used is controlled by `allowHarden()`, which is backed by a committee-controlled chain parameter (`AllowHardenExchangeCalculation`), not something the caller controls; test code explicitly has to opt into the hardened path (`saveAllowHardenExchangeCalculation(1)`), showing the invariant-free legacy path is the default/baseline behavior: [4](#0-3) 

This is analogous to the reported issue: a price/quote computation (`getBuyInfo`/`getSellInfo` in the Solidity report vs. `ExchangeProcessor.exchange` here) can return a value that violates protocol invariants, and the caller (`LSSVMRouter` vs. `ExchangeTransactionActuator`) uses that value to move funds/update state without checking an explicit error condition first — except here, unlike Sudoswap's fix, java-tron's invariant check exists but is conditionally skipped for the legacy/default computation path.

### Impact Explanation
If the legacy double-precision Bancor-style computation ever produces a `buyTokenQuant` that would drive a reserve negative or otherwise inconsistent (e.g., due to floating point edge cases, extreme sell quantities relative to reserves, or precision loss in `Maths.pow`), `ExchangeTransactionActuator.execute()` will still: (1) credit the trader with `anotherTokenQuant` tokens/TRX, and (2) persist a corrupted `ExchangeCapsule` (negative or inconsistent reserve) to the store — since no exception is thrown in the non-hardened branch. This can result in unbacked balance creation (crediting a trader with tokens the pool cannot back) or permanent corruption of the exchange pool's accounting, both of which are fund-loss/fund-integrity issues.

### Likelihood Explanation
Likelihood is Medium: the vulnerable branch (`hardenedCalc == false`) is exercised whenever the `AllowHardenExchangeCalculation` proposal is not active, which per the test suite is the baseline/default configuration validated separately from the "hardened" test paths. Any account holding sufficient balance/asset can broadcast an `ExchangeTransactionContract`; whether a specific input can actually drive the legacy floating-point formula outside the safe invariant requires further numerical analysis of `ExchangeProcessor`'s double-based formula (this repo index does not contain a definitive counterexample proving the negative-balance condition is reachable in practice), so exploitability is not fully confirmed, only the missing-check pattern itself.

### Recommendation
Move the `newFirstTokenBalance < 0 || newSecondTokenBalance < 0` (and any other quote-sanity) check outside of the `hardenedCalc` gate so it always executes for both `ExchangeProcessor` and `SafeExchangeProcessor` results, and throw `ContractValidateException`/abort persistence if violated, regardless of whether the hardened proposal is active.

### Proof of Concept
Not independently confirmed with concrete numeric inputs from the available index; the structural PoC is:
1. Ensure `AllowHardenExchangeCalculation` is disabled (default state).
2. Craft an `ExchangeTransactionContract` with `tokenId`/`quant` chosen so that the legacy `ExchangeProcessor.exchange()` (double/`Maths.pow`-based) returns a `buyTokenQuant` large enough that `secondTokenBalance - buyTokenQuant < 0` (or the symmetric first-token case).
3. Broadcast the transaction; `ExchangeCapsule.transaction()` skips the invariant check (since `hardenedCalc == false`), and `ExchangeTransactionActuator.execute()` persists the negative/corrupted reserve and credits the trader account. [5](#0-4)

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L61-97)
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

**File:** framework/src/test/java/org/tron/core/capsule/ExchangeCapsuleTest.java (L85-106)
```java
  @Test
  public void testTransactionLegacyVsHardenedProcessorSelection() throws Exception {
    // Same input produces deterministic results in both modes.
    ExchangeCapsule legacy = new ExchangeCapsule(
        ByteString.copyFromUtf8("owner"), 100L, 0L,
        "abc".getBytes(), "def".getBytes());
    legacy.setBalance(100_000_000L, 100_000_000L);
    long legacyResult = legacy.transaction("abc".getBytes(), 1_000_000L, true, false);

    ExchangeCapsule hardened = new ExchangeCapsule(
        ByteString.copyFromUtf8("owner"), 101L, 0L,
        "abc".getBytes(), "def".getBytes());
    hardened.setBalance(100_000_000L, 100_000_000L);
    long hardenedResult = hardened.transaction("abc".getBytes(), 1_000_000L, true, true);

    Assert.assertTrue("Both must return positive", legacyResult > 0 && hardenedResult > 0);
    Assert.assertTrue("Hardened must not exceed pool",
        hardenedResult <= 100_000_000L);
    // Allow ±1 difference due to BigDecimal vs double precision
    Assert.assertTrue("Results should be within 1 unit",
        StrictMathWrapper.abs(legacyResult - hardenedResult) <= 1);
  }
```
