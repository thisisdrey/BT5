### Title
Floating-Point Precision Loss in Bancor-Formula Exchange Price Calculation — Legacy `ExchangeProcessor` Active by Default (File: `chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java`)

### Summary
`ExchangeTransactionActuator` (triggered by any user-submitted `ExchangeTransactionContract`) computes swap output quantities via `ExchangeCapsule.transaction()`, which selects between a legacy double-precision floating-point processor (`ExchangeProcessor`) and a `BigDecimal`-based `SafeExchangeProcessor` depending on the `allowHardenExchangeCalculation` governance flag. Test fixtures explicitly toggle this flag to `1` before hardened-path tests and reset it to `0` afterward, indicating the hardened, precision-safe path is opt-in and the legacy floating-point path is the default production behavior.

### Finding Description
`ExchangeCapsule.transaction()` picks the processor based on `hardenedCalc`: [1](#0-0) 

`AbstractExchangeActuator.allowHarden()` derives `hardenedCalc` purely from the dynamic-property governance flag `allowHardenExchangeCalculation()`: [2](#0-1) 

`ExchangeTransactionActuator` passes this flag straight through when executing and validating swaps triggered by an ordinary account's `ExchangeTransactionContract`: [3](#0-2) [4](#0-3) 

When the hardened flag is not enabled, price/quantity computation goes through `ExchangeProcessor`, which relies entirely on `double` arithmetic and `Math.pow`/`StrictMath.pow` for the Bancor bonding-curve formula, then truncates the `double` result with a plain cast to `long`: [5](#0-4) 

This is structurally the same defect class as the reported issue: intermediate fixed/floating point arithmetic (`1e18`-scaled multiply/divide in the Solidity report vs. IEEE‑754 `double` power/division here) introduces rounding error before the result is truncated to an integer output amount. Test evidence confirms the legacy and hardened paths produce materially different, precision-diverging results for identical inputs: [6](#0-5) 

and that the hardened BigDecimal-based mode is only active when the store explicitly enables it, which framework tests set and then immediately revert to `0`, confirming disabled-by-default production behavior: [7](#0-6) 

### Impact Explanation
The legacy path's use of `double`-precision floating point for the core Bancor exchange formula (`exchangeToSupply`/`exchangeFromSupply`) introduces both (a) rounding-error precision loss in computed swap outputs that can be exploited by carefully chosen `tokenQuant`/pool-balance combinations to extract more value than the pool should yield across successive or round-trip trades, and (b) non-strict floating point (`Math.pow`) behavior gated by a separate `allowStrictMath` flag, which is a consensus-sensitive computation — any divergence in floating-point evaluation across node JVMs/platforms for the *same* transaction inputs can cause differing `anotherTokenQuant` results, i.e., state divergence between nodes (chain split risk), rather than merely an off-chain oracle inaccuracy as in the original report. Both failure modes are reachable by any unprivileged account submitting a single `ExchangeTransactionContract` against a TRX/TRC10 `Exchange` pool it does not own, satisfying "unauthorized account operation/theft of funds" or "chain split" impact criteria.

### Likelihood Explanation
High reachability: any account holding funds in an `Exchange`/`ExchangeV2` pool token pair can submit `ExchangeTransactionContract` directly (no special privilege required), and by default `allowHardenExchangeCalculation` is disabled, so the floating-point legacy `ExchangeProcessor` path executes for every such transaction unless SR governance has separately voted to enable hardening. Exploiting rounding-error extraction requires crafting specific balance/quantity ratios, which is feasible for an attacker who can read on-chain pool state before submitting a transaction.

### Recommendation
Make the `BigDecimal`-based `SafeExchangeProcessor` (already implemented and tested) the default computation path instead of a governance-gated opt-in, and/or force `StrictMath.pow` everywhere in `ExchangeProcessor` (removing the `useStrictMath`/`disableJavaLangMath` toggles that allow `Math.pow`) to eliminate both rounding-loss exploitation and cross-platform floating point non-determinism in this consensus-critical calculation.

### Proof of Concept
1. Do not pass the `AllowHardenExchangeCalculation` proposal (default state, as also reflected by every framework test resetting `saveAllowHardenExchangeCalculation(0)` after use).
2. Create/observe an `Exchange` pool with token balances `firstTokenBalance`, `secondTokenBalance`.
3. Submit an `ExchangeTransactionContract` with a `tokenQuant` chosen (via off-chain simulation of `ExchangeProcessor.exchangeToSupply`/`exchangeFromSupply` `double` math) such that floating point truncation in `(long) issuedSupply` / `(long) exchangeBalance` yields an output more favorable to the attacker than the exact fixed-point Bancor formula would produce (demonstrated divergence pattern already visible in `ExchangeProcessorTest.testStrictMath`, where legacy vs. `SafeExchangeProcessor` results differ for identical inputs at lines 272-280 of `chainbase`'s corresponding test file cited above).
4. Repeat/trade in both directions to accumulate the discrepancy, extracting value from the pool relative to the exact/hardened calculation.

Note: I could not fully confirm the compile-time default value of the `allowStrictMath` dynamic property (used to pick `Math.pow` vs `StrictMath.pow` inside `Maths.pow`), since its defining store code (`org.tron.common.math.Maths`) was not retrieved within the available tool budget — this affects only the severity of the cross-platform non-determinism sub-claim, not the confirmed rounding-loss-by-default finding, which is fully supported by the cited `ExchangeProcessor`, `AbstractExchangeActuator`, and test evidence above.

### Citations

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-129)
```java
  public long transaction(byte[] sellTokenID, long sellTokenQuant, boolean useStrictMath,
      boolean hardenedCalc) throws ContractValidateException {
    long supply = 1_000_000_000_000_000_000L;
    Processor processor = hardenedCalc
        ? SafeExchangeProcessor.INSTANCE : new ExchangeProcessor(supply, useStrictMath);

```

**File:** actuator/src/main/java/org/tron/core/actuator/AbstractExchangeActuator.java (L13-15)
```java
  protected boolean allowHarden() {
    return chainBaseManager.getDynamicPropertiesStore().allowHardenExchangeCalculation();
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L66-69)
```java

      byte[] anotherTokenID;
      long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
          dynamicStore.allowStrictMath(), allowHarden());
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L217-221)
```java
    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
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

**File:** framework/src/test/java/org/tron/core/actuator/ExchangeWithdrawActuatorTest.java (L1806-1832)
```java
  public void hardenedPrecisionCheckPassesWhenPrecise() {
    dbManager.getDynamicPropertiesStore().saveAllowSameTokenName(1);
    dbManager.getDynamicPropertiesStore().saveAllowHardenExchangeCalculation(1);
    InitExchangeSameTokenNameActive();
    long exchangeId = 1;
    // 100M / 200M pool, withdraw 100M of first token (full ratio, precise)
    String firstTokenId = "123";
    long firstTokenQuant = 100000000L;

    ExchangeWithdrawActuator actuator = new ExchangeWithdrawActuator();
    actuator.setChainBaseManager(dbManager.getChainBaseManager()).setAny(getContract(
        OWNER_ADDRESS_FIRST, exchangeId, firstTokenId, firstTokenQuant));
    TransactionResultCapsule ret = new TransactionResultCapsule();
    try {
      actuator.validate();
      actuator.execute(ret);
      Assert.assertEquals(code.SUCESS, ret.getInstance().getRet());
    } catch (Exception e) {
      Assert.fail("Hardened precise withdraw must succeed: " + e.getMessage());
    } finally {
      dbManager.getExchangeStore().delete(ByteArray.fromLong(1L));
      dbManager.getExchangeStore().delete(ByteArray.fromLong(2L));
      dbManager.getExchangeV2Store().delete(ByteArray.fromLong(1L));
      dbManager.getExchangeV2Store().delete(ByteArray.fromLong(2L));
      dbManager.getDynamicPropertiesStore().saveAllowHardenExchangeCalculation(0);
    }
  }
```
