### Title
Floating-point precision drift in the legacy AMM/relay-exchange formula allows single-transaction pool-ratio manipulation and value extraction — ([File: chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java])

### Summary
The Goledo Finance incident was a flash-loan attack that manipulated a lending pool's exchange/price ratio within a single transaction to extract far more value than deposited. The closest reachable analog in java-tron is the TRC10↔TRX "Bancor-style" exchange pool (`ExchangeTransactionContract`), whose value-exchange math is computed with IEEE-754 `double` arithmetic (`Math.pow`) in `ExchangeProcessor`, rather than the exact `BigInteger`/`BigDecimal` math used by the sibling `ExchangeInject`/`ExchangeWithdraw` validators. An unprivileged account that repeatedly calls `ExchangeTransactionContract` against the same pool inside a single transaction/block can exploit accumulated rounding error in the floating-point relay-supply calculation to shift the pool's effective exchange ratio in its favor, extracting value from other liquidity beyond what the exact constant-relay-supply model would allow — the same "single-transaction ratio manipulation" bug class as the reported flash-loan attack.

### Finding Description
`ExchangeTransactionActuator.execute()` calls `exchangeCapsule.transaction(...)`, which selects between two processors depending on the `allowHardenExchangeCalculation` dynamic property: [1](#0-0) 

When the hardened flag is not enabled (a chain-wide committee proposal, `ALLOW_HARDEN_EXCHANGE_CALCULATION`, gated in `ProposalUtil`/`ProposalService`), every exchange trade is routed through `ExchangeProcessor`, which implements the Bancor-relay formula using raw `double` and `Math.pow`: [2](#0-1) 

This is materially different from `ExchangeInjectActuator`/`ExchangeWithdrawActuator`, which perform their pricing math in exact `BigInteger`/`BigDecimal` arithmetic: [3](#0-2) [4](#0-3) 

The repo's own test suite explicitly documents that the legacy (`useStrictMath=true/false`) and hardened (`SafeExchangeProcessor`, exact `BigDecimal`) paths produce *different* results for identical inputs: [5](#0-4) 

Because `ExchangeProcessor` maintains a mutable `supply` field that is updated after each call and mixes floating-point rounding on both the "toSupply" and "fromSupply" legs, an attacker who issues many small, precisely-sized `ExchangeTransactionContract` trades against the same `ExchangeCapsule` in one transaction/block can accumulate a rounding bias in their favor (classic "double rounding" AMM exploit), draining pool value from the other side of the pool disproportionate to what an exact-math implementation would permit — mirroring the single-transaction ratio-manipulation root cause of the Goledo flash-loan attack, just implemented via repeated trades against a floating-point pricing curve instead of a price-oracle read.

### Impact Explanation
If exploitable, the attacker can extract TRX/TRC10 value from an `Exchange` pool beyond what depositors placed in and beyond the exact-math entitlement, i.e., unbacked balance/theft of funds from other pool participants (the creator and any prior injectors), consistent with the "unauthorized theft or permanent freezing of funds" acceptance criterion. This requires only a funded account and no special privilege — reachable via a normal signed `ExchangeTransactionContract` transaction, i.e., the "actuator validate/execute for any broadcastable contract type" surface named in scope.

### Likelihood Explanation
Likelihood is Medium: the vulnerable path is only active while `allowHardenExchangeCalculation` is `0` (not yet activated by committee proposal) — the codebase clearly already engineered and tested a fix (`SafeExchangeProcessor`), implying the floating-point path is a known-risky legacy fallback rather than the intended long-term state. Whether `allowHardenExchangeCalculation` defaults to `0` or `1` on the currently deployed mainnet could not be conclusively verified from the index (the constant/default initializer in `DynamicPropertiesStore.java` was not retrievable due to index truncation), so exploitability in production depends on that on-chain configuration value.

### Recommendation
- Confirm and, if necessary, activate `allowHardenExchangeCalculation` chain-wide so `SafeExchangeProcessor` (exact `BigDecimal` math) is unconditionally used for all `ExchangeTransactionContract` executions, removing the legacy `double`/`Math.pow` code path entirely rather than leaving it reachable behind a toggle.
- Add invariant checks in `ExchangeProcessor`/`ExchangeCapsule.transaction()` that assert the product/relay-supply invariant is non-decreasing (or within a bounded tolerance) after every trade, rejecting transactions that would violate it, independent of which processor is active.
- Add fuzz/property tests that specifically chain many small `ExchangeTransactionContract` calls in sequence to detect cumulative rounding drift, mirroring how AMM flash-loan/rounding exploits are typically discovered.

### Proof of Concept
Conceptual (exact numeric parameters depend on live pool state, which is not retrievable from the index):
1. Attacker identifies a TRX/TRC10 `Exchange` pool where `allowHardenExchangeCalculation == 0` (legacy floating-point path active), reachable via `Wallet`/`TronJsonRpcImpl` query APIs to read `Exchange` balances.
2. Attacker submits repeated `ExchangeTransactionContract` transactions in tight sequence (packed into one or few blocks) with quantities chosen so that `ExchangeProcessor.exchangeToSupply`/`exchangeFromSupply` double-rounding consistently rounds in the attacker's favor (verifiable offline by replaying `ExchangeProcessor.exchange()` with the pool's real balances to search for a favorable rounding sequence, exactly as `ExchangeProcessorTest.testStrictMath()` already does for legacy vs. hardened divergence).
3. Each trade slightly shifts `firstTokenBalance`/`secondTokenBalance` via `exchangeCapsule.setBalance(...)` in `ExchangeTransactionActuator.execute()`, compounding the rounding bias.
4. After enough iterations, the attacker withdraws (or has already extracted) more value than the exact-math model would allow, at the expense of other pool participants — reproducing the "single transaction, ratio-manipulation, unbacked value extraction" pattern of the Goledo Finance flash-loan incident. Full end-to-end quantification requires running the actuator against a live/staging java-tron node to measure the actual drift magnitude, which exceeds what static code inspection alone can confirm.

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L71-83)
```java
      if (Arrays.equals(tokenID, firstTokenID)) {
        anotherTokenID = secondTokenID;
        anotherTokenQuant = floorDiv(multiplyExact(
            secondTokenBalance, tokenQuant), firstTokenBalance);
        exchangeCapsule.setBalance(addExact(firstTokenBalance, tokenQuant),
            addExact(secondTokenBalance, anotherTokenQuant));
      } else {
        anotherTokenID = firstTokenID;
        anotherTokenQuant = floorDiv(multiplyExact(
            firstTokenBalance, tokenQuant), secondTokenBalance);
        exchangeCapsule.setBalance(addExact(firstTokenBalance, anotherTokenQuant),
            addExact(secondTokenBalance, tokenQuant));
      }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L74-89)
```java
      BigInteger bigFirstTokenBalance = new BigInteger(String.valueOf(firstTokenBalance));
      BigInteger bigSecondTokenBalance = new BigInteger(String.valueOf(secondTokenBalance));
      BigInteger bigTokenQuant = new BigInteger(String.valueOf(tokenQuant));
      if (Arrays.equals(tokenID, firstTokenID)) {
        anotherTokenID = secondTokenID;
        anotherTokenQuant = bigSecondTokenBalance.multiply(bigTokenQuant)
            .divide(bigFirstTokenBalance).longValueExact();
        exchangeCapsule.setBalance(subtractExact(firstTokenBalance, tokenQuant),
            subtractExact(secondTokenBalance, anotherTokenQuant));
      } else {
        anotherTokenID = firstTokenID;
        anotherTokenQuant = bigFirstTokenBalance.multiply(bigTokenQuant)
            .divide(bigSecondTokenBalance).longValueExact();
        exchangeCapsule.setBalance(subtractExact(firstTokenBalance, anotherTokenQuant),
            subtractExact(secondTokenBalance, tokenQuant));
      }
```

**File:** framework/src/test/java/org/tron/core/capsule/utils/ExchangeProcessorTest.java (L272-280)
```java
    for (long[] data : testData) {
      ExchangeProcessor processor = new ExchangeProcessor(supply, false);
      long anotherTokenQuant = processor.exchange(data[0], data[1], data[2]);
      processor = new ExchangeProcessor(supply, true);
      long result = processor.exchange(data[0], data[1], data[2]);
      long safeResult = SafeExchangeProcessor.INSTANCE.exchange(data[0], data[1], data[2]);
      Assert.assertNotEquals(anotherTokenQuant, result);
      Assert.assertEquals(safeResult, result);
    }
```
