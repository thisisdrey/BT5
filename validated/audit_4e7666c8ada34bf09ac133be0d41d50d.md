### Title
Floating-point precision loss in the TRC10 Bancor-style Exchange AMM math allows value extraction from swap pools - (File: `chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java`)

### Summary
The default (non-hardened) `Exchange` AMM curve implementation performs its bonding-curve math with Java `double` arithmetic and truncates the result to `long` with a raw cast. This mirrors the Formation.Fi root cause: the protocol underestimates the impact of rounding/precision loss on the pool's internal accounting ("totalTokens"/reserve balances) when tokens of different scale are swapped through the curve, letting a caller who issues a sequence of ordinary swap transactions against `ExchangeTransactionActuator` extract value the invariant is supposed to protect. The java-tron team's own later mitigation — `SafeExchangeProcessor` (BigDecimal, `RoundingMode.HALF_UP`, `18`-digit scale) gated behind the `allowHardenExchangeCalculation` proposal flag — is direct evidence that the legacy `double`-based path is the vulnerable one, and it remains the default behavior until the proposal is activated.

### Finding Description
`ExchangeCapsule.transaction()` selects the processor used to price a swap: [1](#0-0) 

When `hardenedCalc` is `false` (the default, pre-proposal-activation state), it uses `ExchangeProcessor`, whose Bancor relay math is computed entirely in floating point and then hard-truncated to `long`: [2](#0-1) 

Both `exchangeToSupply` and `exchangeFromSupply` compute `Maths.pow(...)` on `double` values and cast the result directly to `long` (`(long) issuedSupply`, `(long) exchangeBalance`), discarding any error accumulated in the double-precision power/division operations. Because a full round trip (sell token A for the "relay supply", then sell the relay supply back for token A) is mathematically supposed to approximately cancel (`0.0005 * 2000 = 1`), any systematic or input-dependent bias introduced by IEEE-754 rounding in `Math.pow`/`StrictMathWrapper.pow` is not bounded by an invariant check — there is no `k = x*y` (or equivalent) reserve-conservation assertion after the trade, unlike the hardened path which at least prevents negative balances: [3](#0-2) 

`ExchangeTransactionActuator`, which is reachable by any account holding the relevant TRC10 token or TRX, calls this pricing function directly with no additional sanity check on the resulting `anotherTokenQuant` beyond "greater than the caller-specified minimum": [4](#0-3) [5](#0-4) 

Notably, `calcFee()` for this actuator is `0`: [6](#0-5) 

so an attacker can issue an unbounded number of round-trip swaps against the same pool at essentially only bandwidth cost, repeatedly probing for input sizes where floating-point rounding biases the output in the attacker's favor — the same "the project underestimated the impact of fee/precision on totalTokens" pattern described in the Formation.Fi report, just applied to java-tron's own bonding-curve reserve accounting instead of an EVM contract's `swapIn`.

### Impact Explanation
If an attacker identifies (or the `Maths.pow`/`double` rounding behavior structurally produces) input sizes for which the truncated relay-supply/output computation yields a net gain across a sell-then-buy-back cycle, the attacker can drain real TRX/TRC10 reserves from the `Exchange` pool into their own account while the exchange's on-chain balances are updated to reflect a curve that no longer matches the true conserved-value invariant. This is a direct theft-of-funds / unbacked-balance condition on a production-reachable, unprivileged transaction path (any TRX/TRC10 holder can call `ExchangeTransactionContract`), warranting Medium-severity classification, consistent with the referenced incident.

### Likelihood Explanation
The path is reachable by a single unprivileged, signed `ExchangeTransactionContract` transaction with zero actuator fee, so an attacker can iterate quickly and cheaply to search for favorable input sizes. However, exploitability depends on actually finding an input where the double-precision truncation biases the output beyond the pool's own protective truncation-toward-zero behavior in `exchangeToSupply`/`exchangeFromSupply` (both truncations nominally favor the pool, not the trader), so a concrete profitable sequence has not been proven in this codebase — it requires empirical/differential testing of `Maths.pow`/`StrictMathWrapper.pow` outputs across the relevant input ranges to confirm a monetizable bias exists, similar to how the original Formation.Fi bug required a specific sequence of flash-loaned deposits to become profitable.

### Recommendation
- Make the `SafeExchangeProcessor` (BigDecimal-based, `RoundingMode.HALF_UP`) the default and only implementation for the `Exchange` curve rather than gating it behind the `allowHardenExchangeCalculation` proposal, since the `double`-based `ExchangeProcessor` remains reachable and is the historically weaker implementation.
- Add an explicit reserve/invariant check after every `ExchangeCapsule.transaction()` call (e.g., verifying the new relay supply and output do not exceed theoretically bounded values), independent of which processor is used.
- Add fuzz/differential tests comparing `ExchangeProcessor` (double) vs `SafeExchangeProcessor` (BigDecimal) outputs across a wide range of `sellTokenBalance`/`buyTokenBalance`/`sellTokenQuant` combinations to detect and quantify any exploitable bias before deciding it is safe to leave `ExchangeProcessor` reachable at all.

### Proof of Concept
Not independently verified with a concrete profitable sequence — this report identifies and documents the reachable, root-caused code path (`ExchangeTransactionActuator` → `ExchangeCapsule.transaction()` → `ExchangeProcessor.exchangeToSupply/exchangeFromSupply`) and its structural analogy to the Formation.Fi precision-underestimation bug; confirming a specific attacker-controlled input sequence that yields net profit requires running/fuzzing the `Maths.pow`/`StrictMathWrapper.pow` double-precision computations across realistic reserve ranges, which was outside the scope of static code review available here.

### Citations

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-129)
```java
  public long transaction(byte[] sellTokenID, long sellTokenQuant, boolean useStrictMath,
      boolean hardenedCalc) throws ContractValidateException {
    long supply = 1_000_000_000_000_000_000L;
    Processor processor = hardenedCalc
        ? SafeExchangeProcessor.INSTANCE : new ExchangeProcessor(supply, useStrictMath);

```

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L160-168)
```java
    if (hardenedCalc && (newFirstTokenBalance < 0 || newSecondTokenBalance < 0)) {
      throw new ContractValidateException("Exchange balance must be >=0 after transaction");
    }
    this.exchange = this.exchange.toBuilder()
        .setFirstTokenBalance(newFirstTokenBalance)
        .setSecondTokenBalance(newSecondTokenBalance)
        .build();

    return buyTokenQuant;
```

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java (L17-39)
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
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L61-99)
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

      ret.setExchangeReceivedAmount(anotherTokenQuant);
      ret.setStatus(fee, code.SUCESS);
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L217-221)
```java
    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L232-235)
```java
  @Override
  public long calcFee() {
    return 0;
  }
```
