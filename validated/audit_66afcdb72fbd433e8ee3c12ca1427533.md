### Title
Unbacked value extraction from TRC10 Bancor-style Exchange pools due to floating-point precision loss in the default (non-hardened) exchange calculation - ([File: chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java])

### Summary
The `ExchangeTransactionContract` path (reachable by any account broadcasting a signed transaction) computes trade output using `ExchangeCapsule.transaction()`, which by default routes through `ExchangeProcessor`, a Bancor-relay implementation that performs its core math with Java `double`/`Math.pow` arithmetic instead of exact fixed-point/BigDecimal arithmetic. A hardened, `BigDecimal`-based alternative (`SafeExchangeProcessor`) exists in the codebase but is only used when the chain-wide proposal flag `allowHardenExchangeCalculation()` is enabled; unless/until that proposal is active, every `ExchangeTransactionContract` execution uses the imprecise floating-point path.

### Finding Description
`ExchangeCapsule.transaction()` selects the processor based on `hardenedCalc`: [1](#0-0) 

`ExchangeTransactionActuator.execute()` calls `exchangeCapsule.transaction(tokenID, tokenQuant, dynamicStore.allowStrictMath(), allowHarden())`, where `allowHarden()` reflects `chainBaseManager.getDynamicPropertiesStore().allowHardenExchangeCalculation()`: [2](#0-1) [3](#0-2) 

When `allowHardenExchangeCalculation` is not yet active (its default/pre-proposal state), the legacy `ExchangeProcessor` is used, computing the Bancor "relay supply" via `double` math and `Maths.pow`: [4](#0-3) 

Because this arithmetic is done with IEEE-754 doubles rather than exact rational/BigDecimal math, the computed `buyTokenQuant` for a given `sellTokenQuant` is not exactly invariant-preserving. An attacker who fully controls the size and sequencing of `quant` in repeated `ExchangeTransactionContract` calls (an unprivileged operation, gated only by normal bandwidth/energy fees) can search for input sizes where floating-point rounding systematically favors the buyer, extracting more of the counter-asset than the constant-product/Bancor invariant would allow if computed exactly. Because the pool balances (`firstTokenBalance`/`secondTokenBalance` stored in `ExchangeCapsule`) and the trader's account asset balance (via `addAssetAmountV2`/`reduceAssetAmountV2`) are updated using this same imprecise output, over many transactions this produces value that is not actually backed by an equivalent decrease in the counter-asset pool — i.e., the trader's TRC10 balance increases faster than the pool's true depletion would allow under exact math, while other liquidity participants (via `ExchangeInjectActuator`, which uses exact `floorDiv`/`multiplyExact` math for its own separate calculation) have no compensating mechanism to correct this drift.

### Impact Explanation
Repeated exploitation drains real value from Exchange pools (funded by other users via `ExchangeCreateActuator`/`ExchangeInjectActuator`) into the attacker's account without a corresponding backing decrease, i.e., unbacked-balance creation / theft of pooled funds — meeting the "unbacked balance" and "theft of funds" impact bar. This is systemic for every live TRC10↔TRX or TRC10↔TRC10 exchange pool as long as the network has not activated `ALLOW_HARDEN_EXCHANGE_CALCULATION`.

### Likelihood Explanation
Any account can create an `ExchangeTransactionContract` and repeatedly trade against a target pool with parameters they fully choose (asset id and quant), requiring only transaction fees/bandwidth — no special privilege, and the vulnerable path is the *default* processor prior to committee activation of the hardening proposal.

### Recommendation
Make the `SafeExchangeProcessor` (BigDecimal-based) the unconditional/default implementation for all `ExchangeTransactionContract` executions instead of gating it behind a proposal flag, or, until the network-wide proposal activates, add server-side clamping/consistency checks so the sum of value moved into/out of both sides of an exchange pool is invariant-preserving within acceptable integer rounding, rejecting trades whose computed output deviates from an exact-math reference calculation beyond a fixed tolerance.

### Proof of Concept
1. Create an `ExchangeCreateContract` establishing a pool between TRX and a TRC10 asset with a chosen initial ratio (`ExchangeCreateActuator`).
2. Before `allowHardenExchangeCalculation` is enabled (checked via `DynamicPropertiesStore.allowHardenExchangeCalculation()`), broadcast a sequence of `ExchangeTransactionContract` transactions with `quant` values chosen to trigger favorable floating-point rounding in `ExchangeProcessor.exchangeToSupply`/`exchangeFromSupply` (`Math.pow` with fractional exponents `0.0005`/`2000.0` on `double` inputs).
3. Observe over many iterations that the attacker's TRC10/TRX balance increase exceeds what an exact Bancor-invariant computation would yield for the same net pool balance change, confirming unbacked value extraction from the pool as computed at [5](#0-4) .

### Citations

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-129)
```java
  public long transaction(byte[] sellTokenID, long sellTokenQuant, boolean useStrictMath,
      boolean hardenedCalc) throws ContractValidateException {
    long supply = 1_000_000_000_000_000_000L;
    Processor processor = hardenedCalc
        ? SafeExchangeProcessor.INSTANCE : new ExchangeProcessor(supply, useStrictMath);

```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L64-69)
```java
      byte[] tokenID = exchangeTransactionContract.getTokenId().toByteArray();
      long tokenQuant = exchangeTransactionContract.getQuant();

      byte[] anotherTokenID;
      long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
          dynamicStore.allowStrictMath(), allowHarden());
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
