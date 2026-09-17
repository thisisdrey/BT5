## Title
Exchange pool token-balance invariant (`firstTokenBalance`/`secondTokenBalance`) can underflow/go negative from bonding-curve rounding drift, freezing trades or persisting an unbacked balance - (File: `chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java`)

### Summary
`ExchangeCapsule.transaction()` maintains two on-chain counters, `firstTokenBalance` and `secondTokenBalance`, that are supposed to always mirror the real reserves of a TRC10/TRX bonding-curve pool. These counters are updated by amounts computed from a bancor-style formula (`ExchangeProcessor`/`SafeExchangeProcessor`), not by directly reading the authoritative reserve — the exact "computed decrement vs. recorded total" pattern that caused `PerpDepository.netAssetDeposits` to underflow in the referenced report.

### Finding Description
In `transaction()` [1](#0-0) , `buyTokenQuant` is derived from a floating-point/BigDecimal bonding-curve computation (`processor.exchange(...)`) and then subtracted from the pool's stored integer balance. The two computation paths behave differently:

- **Legacy (non-hardened) path** — `newSecondTokenBalance = secondTokenBalance - buyTokenQuant;` (plain `long` arithmetic, no bounds check) is written straight back into the capsule with no `>=0` validation [2](#0-1) . If accumulated floating-point drift (double `Maths.pow`) ever causes the computed `buyTokenQuant` to exceed the real reserve, the negative value is persisted directly into `ExchangeStore`/`ExchangeV2Store`, corrupting the pool's invariant permanently (unbacked/negative reserve state feeds every subsequent trade's math).
- **Hardened path** — explicitly guards this with `if (hardenedCalc && (newFirstTokenBalance < 0 || newSecondTokenBalance < 0)) { throw new ContractValidateException(...) }` [3](#0-2) . This converts the corruption into a hard revert for that trade direction — functionally identical to `PerpDepository._withdrawAsset` reverting when `amount > netAssetDeposits`.

Which path executes is controlled by a committee-toggled chain parameter, `allowHardenExchangeCalculation()` [4](#0-3) , reachable from ordinary user transactions via `ExchangeTransactionActuator.execute()`/`doValidate()`, which any unprivileged "order placer" can send [5](#0-4) .

A unit test even demonstrates the hardened invariant tripping on a corrupted-state pool, confirming the negative-balance condition is a recognized, reachable state [6](#0-5) .

### Impact Explanation
- Under the legacy math path (the historical default before the hardening proposal is activated by committee vote), a sequence of trades that drives the bonding-curve computed `buyTokenQuant` above the real stored reserve results in a **negative reserve value silently persisted on-chain**, an unbacked-balance condition that corrupts all future pricing/settlement on that exchange pool.
- Under the hardened path, the same condition instead makes legitimate sell/buy transactions in one direction **permanently revert** with `"Exchange balance must be >=0 after transaction"`, freezing users' ability to trade against the pool until an offsetting trade in the opposite direction restores the invariant — mirroring the "self-correcting Medium" characterization accepted in the referenced Sherlock issue.

### Likelihood Explanation
This requires accumulated rounding/precision drift between the floating-point bonding-curve model and the integer-stored reserves across a sequence of ordinary `ExchangeTransactionContract` calls — no special privilege, malicious SR/witness, or network-level attack is needed, only repeated normal usage of the public Exchange feature, consistent with the "edge case, self-correcting" Medium classification of the original finding.

### Recommendation
Apply the same invariant check unconditionally regardless of `allowHardenExchangeCalculation()`, i.e. always validate `newFirstTokenBalance >= 0 && newSecondTokenBalance >= 0` before persisting the capsule in the legacy path too, and consider clamping/rejecting trades that would exceed the recorded reserve rather than relying purely on the bonding-curve formula's output.

### Proof of Concept
1. An unprivileged account creates/uses an existing TRC10↔TRX Exchange pool and repeatedly issues `ExchangeTransactionContract` trades (`ExchangeTransactionActuator`) in one direction.
2. Each trade calls `ExchangeCapsule.transaction()`, which computes `buyTokenQuant` via floating-point bonding-curve math and updates `firstTokenBalance`/`secondTokenBalance` by plain subtraction [7](#0-6) .
3. As precision drift accumulates (or with a large sell just before reserve depletion), a subsequent trade's computed `buyTokenQuant` exceeds the actual remaining `secondTokenBalance`.
4. On the legacy path, the negative balance is written directly into the store with no check; on the hardened path, the transaction reverts with `ContractValidateException("Exchange balance must be >=0 after transaction")`, blocking that trade direction until a reverse trade rebalances the pool.

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

**File:** framework/src/test/java/org/tron/core/capsule/ExchangeCapsuleTest.java (L71-83)
```java
  @Test
  public void testHardenedTransactionNegativeBalanceThrows() throws Exception {
    // Construct a corrupt-state pool with a negative balance to drive the
    // < 0 invariant in the hardened branch via subtractExact wrapping.
    ExchangeCapsule capsule = new ExchangeCapsule(
        ByteString.copyFromUtf8("owner"), 99L, 0L,
        "abc".getBytes(), "def".getBytes());
    capsule.setBalance(Long.MAX_VALUE, 1L);

    // Selling abc adds to firstTokenBalance: addExact(MAX, q) overflows -> ArithmeticException
    Assert.assertThrows(ArithmeticException.class,
        () -> capsule.transaction("abc".getBytes(), 1L, true, true));
  }
```
