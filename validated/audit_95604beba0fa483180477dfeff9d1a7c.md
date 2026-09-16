Based on my investigation, I found a concrete analog in java-tron's `ExchangeTransactionActuator` / `ExchangeCapsule` flow that mirrors the "asset moved out without a repayment/solvency check" bug class from the report.

### Title
Exchange pool balances can be validated and updated without a non-negative solvency check when `ALLOW_HARDEN_EXCHANGE_CALCULATION` is disabled - ([File: chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java])

### Summary
The reported bug is that a `Facilitator` in `AdvancedOrderEngine` is handed borrowed assets by `_processFacilitatorInteraction` with no code path verifying repayment before the transaction completes. The analogous root cause in java-tron is `ExchangeCapsule.transaction()`, which computes and commits new pool reserves (`newFirstTokenBalance`/`newSecondTokenBalance`) for a bonded-curve exchange, but only asserts the reserves stay non-negative when the `hardenedCalc` flag is true. `ALLOW_HARDEN_EXCHANGE_CALCULATION` defaults to `0` and can only be turned on via a committee proposal, so by default the "solvency" check that assets paid out never exceed assets available is skipped entirely, just as the audited contract skipped verifying that borrowed assets were returned.

### Finding Description
`ExchangeTransactionActuator.execute()` calls `exchangeCapsule.transaction(tokenID, tokenQuant, dynamicStore.allowStrictMath(), allowHarden())`, and then unconditionally credits `anotherTokenQuant` to the caller's account and persists the updated `ExchangeCapsule`: [1](#0-0) 

Inside `ExchangeCapsule.transaction()`, the new reserve balances are computed from the constant-product-like `ExchangeProcessor`/`SafeExchangeProcessor`, but the guard that rejects a negative resulting balance is gated behind `hardenedCalc`: [2](#0-1) 

`allowHarden()` reads `DynamicPropertiesStore.allowHardenExchangeCalculation()`, which is backed by the `ALLOW_HARDEN_EXCHANGE_CALCULATION` chain parameter, defaulting to `0` unless a committee proposal explicitly enables it: [3](#0-2) [4](#0-3) 

The only remaining safeguard when hardening is off is the doubles/BigInteger-based `ExchangeProcessor` math itself and the `firstTokenBalance == 0 || secondTokenBalance == 0` closed-market check in validation — there is no explicit assertion, in the non-hardened path, that `newFirstTokenBalance >= 0` or `newSecondTokenBalance >= 0` before the pool state is committed and the counter-asset is paid out to the user. This is structurally identical to the reported issue: value leaves the accounted pool (`_sendAsset`/`addAssetAmountV2`) without a corresponding check that the pool's books remain solvent/balanced, unless an opt-in "hardened" flag is enabled.

### Impact Explanation
If floating-point rounding or extreme reserve ratios in the non-hardened `ExchangeProcessor` (which uses `double`/`Math.pow` per `ChainbaseManager`/`ExchangeProcessorTest.testStrictMath` comparisons showing hardened vs strict-math results diverge) ever yield an `anotherTokenQuant` that exceeds available reserves, `ExchangeTransactionActuator` will still pay it out and persist a negative or inconsistent pool balance, since the negative-balance guard only fires under `hardenedCalc`. This is not an ordinary user-facing DoS: it can result in a market maker (Exchange) pool becoming permanently insolvent (unbacked balance) — tokens credited to a caller's account that are not backed by real reserves, which the auditors' concern class ("assets moved without solvency verification") maps to directly.

### Likelihood Explanation
Reachability is straightforward: any account can submit a signed `ExchangeTransactionContract` transaction hitting `ExchangeTransactionActuator.execute()`; no special privilege is required (rule allows "order placer"/unprivileged broadcaster). Exploiting the missing check requires crafting reserve ratios/quantities that push the legacy double-based math into producing an over-large `anotherTokenQuant`; this depends on specific numeric edge cases (extreme token supply/precision combinations) rather than being trivially triggerable on arbitrary inputs, so likelihood is bounded by the difficulty of finding such an edge case in the legacy `ExchangeProcessor`, but the validate()-time precision checks (e.g., in `ExchangeWithdrawActuator`) suggest such precision issues have historically been a real source of concern in this exact subsystem.

### Recommendation
Move the non-negative reserve-balance assertion in `ExchangeCapsule.transaction()` out of the `hardenedCalc`-only branch so it always executes regardless of the `ALLOW_HARDEN_EXCHANGE_CALCULATION` flag, ensuring `ExchangeTransactionActuator` (and other exchange actuators using this method) can never commit or pay out against an insolvent pool state.

### Proof of Concept
Not independently reproducible from the indexed code alone: doing so would require identifying concrete `(firstTokenBalance, secondTokenBalance, sellTokenQuant)` inputs under the legacy `ExchangeProcessor` double-based math that make `newFirstTokenBalance` or `newSecondTokenBalance` go negative while `hardenedCalc` is false (default). The existing test suite (`ExchangeProcessorTest.testStrictMath`) already demonstrates that hardened and non-hardened math diverge for real inputs, which supports pursuing this further with a dedicated fuzzing session against `ExchangeCapsule.transaction()` in non-hardened mode.

### Citations

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

**File:** actuator/src/main/java/org/tron/core/actuator/AbstractExchangeActuator.java (L13-15)
```java
  protected boolean allowHarden() {
    return chainBaseManager.getDynamicPropertiesStore().allowHardenExchangeCalculation();
  }
```

**File:** chainbase/src/main/java/org/tron/core/store/DynamicPropertiesStore.java (L3059-3072)
```java
  public long getAllowHardenExchangeCalculation() {
    return Optional.ofNullable(getUnchecked(ALLOW_HARDEN_EXCHANGE_CALCULATION))
        .map(BytesCapsule::getData)
        .map(ByteArray::toLong)
        .orElse(0L);
  }

  public void saveAllowHardenExchangeCalculation(long value) {
    this.put(ALLOW_HARDEN_EXCHANGE_CALCULATION, new BytesCapsule(ByteArray.fromLong(value)));
  }

  public boolean allowHardenExchangeCalculation() {
    return getAllowHardenExchangeCalculation() == 1L;
  }
```
