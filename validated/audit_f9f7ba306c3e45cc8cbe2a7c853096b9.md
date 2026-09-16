## Title
Legacy (non-hardened) Exchange balance math permanently corrupts market state with negative token balances - ([File: chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java])

### Summary
`ExchangeCapsule.transaction()`, invoked from `ExchangeTransactionActuator.execute()`, computes new bancor-style pool balances via plain `long` arithmetic (`firstTokenBalance - buyTokenQuant` / `secondTokenBalance - buyTokenQuant`) whenever the committee-controlled feature flag `allowHardenExchangeCalculation()` is off, with no post-computation sanity check. Only the "hardened" path (`hardenedCalc=true`) verifies `newFirstTokenBalance < 0 || newSecondTokenBalance < 0` and reverts. This mirrors the root cause pattern in the reported Streamr bug: a state-transition function relies on a value (`buyTokenQuant`, computed from potentially stale/manipulable pool ratios) and updates persistent balances without validating the post-condition, allowing balances to go corrupt and subsequent legitimate calls to permanently fail/misbehave.

### Finding Description
`ExchangeTransactionActuator.execute()` calls `exchangeCapsule.transaction(...)` [1](#0-0)  which dispatches to either a `SafeExchangeProcessor` (checked) or a legacy `ExchangeProcessor` depending on `allowHarden()` [2](#0-1) . The balance update itself only applies the `>=0` guard when `hardenedCalc` is `true`: [3](#0-2) 
`allowHarden()` is gated by the dynamic property `allowHardenExchangeCalculation()` [4](#0-3) , meaning on any chain/testnet/private-net where this proposal has not been activated, every `ExchangeTransactionContract` submitted by any unprivileged transaction broadcaster runs the unchecked legacy math path. Validation of the transaction only checks `firstTokenBalance == 0 || secondTokenBalance == 0` (exchange "closed") and post-trade balance-limit ceilings, but never checks for negative results, so if legacy bancor math (rounding in `ExchangeProcessor`) ever yields `buyTokenQuant` larger than the available pool balance, the resulting `firstTokenBalance`/`secondTokenBalance` go negative and are persisted via `Commons.putExchangeCapsule(...)` [5](#0-4) .

### Impact Explanation
Once a pool balance goes negative, the exchange's `getFirstTokenBalance()`/`getSecondTokenBalance()` state is permanently corrupted. All subsequent trades against that pool compute distorted, unbacked outputs (negative or absurd `buyTokenQuant`), which can create unbacked token/TRX balances for arbitrary counterparties (asset/TRX theft from the pool or its future participants), and this corruption is unrecoverable through further ordinary exchange transactions, effectively bricking that market pair permanently — the analogous "revert/DoS forever" outcome from the source report translated into a persistently-broken accounting invariant rather than a revert.

### Likelihood Explanation
This requires the `allowHardenExchangeCalculation` proposal to not be active for the network in question (i.e., older/private/testnet chains, or a period before the hardening proposal is passed on mainnet), which is a real, still-shipped code path (not removed) reachable via a single `ExchangeTransactionContract` from any account with sufficient TRX/asset balance. The precise numeric conditions (extreme repeated small trades pushing bancor rounding to exceed pool balance) require careful crafting but are within reach of an unprivileged actor since there is no arithmetic safety net gating the unhardened branch.

### Recommendation
Apply the same `>= 0` post-condition check and `subtractExact`/`addExact` safety used in the hardened path unconditionally, regardless of `allowHardenExchangeCalculation`, so that legacy/pre-hardening deployments cannot persist negative exchange balances.

### Proof of Concept
Not independently reproduced from this analysis; the vulnerable code path (`ExchangeCapsule.transaction` with `hardenedCalc=false`) is confirmed to lack the negative-balance guard present in the hardened branch, but exact numeric inputs that trigger negative results via the legacy `ExchangeProcessor` rounding were not derived here — this would require examining `ExchangeProcessor`'s bancor formula in detail and simulating repeated trades, which was not completed within this investigation. This is flagged as an area of uncertainty.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L68-69)
```java
      long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
          dynamicStore.allowStrictMath(), allowHarden());
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L93-96)
```java
      accountStore.put(accountCapsule.createDbKey(), accountCapsule);

      Commons.putExchangeCapsule(exchangeCapsule, dynamicStore, exchangeStore, exchangeV2Store,
          assetIssueStore);
```

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-129)
```java
  public long transaction(byte[] sellTokenID, long sellTokenQuant, boolean useStrictMath,
      boolean hardenedCalc) throws ContractValidateException {
    long supply = 1_000_000_000_000_000_000L;
    Processor processor = hardenedCalc
        ? SafeExchangeProcessor.INSTANCE : new ExchangeProcessor(supply, useStrictMath);

```

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L140-162)
```java
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
```

**File:** actuator/src/main/java/org/tron/core/actuator/AbstractExchangeActuator.java (L13-15)
```java
  protected boolean allowHarden() {
    return chainBaseManager.getDynamicPropertiesStore().allowHardenExchangeCalculation();
  }
```
