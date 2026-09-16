### Title
Unsafe raw arithmetic in default `ExchangeProcessor` bonding-curve math allows silent overflow of TRC10/TRX exchange balances - (File: `chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java`)

### Summary
`RubiconMarket.sol` performs raw `*` multiplications by `1 ether`/`10**9` instead of the safe `DSMath.mul` already available in the codebase, risking silent overflow of purchase/sale amounts. The analogous java-tron path is the default (non-hardened) `ExchangeProcessor` used by `ExchangeTransactionActuator` when processing an `ExchangeTransactionContract` — an order/trade transaction any unprivileged account can broadcast. This processor performs the bonding-curve math with raw `long` arithmetic and unchecked `double` multiplication instead of the already-existing safe/checked alternative (`SafeExchangeProcessor`), and is only bypassed when a chain-wide `AllowHardenExchangeCalculation` proposal is active.

### Finding Description
`ExchangeCapsule.transaction()` selects between two implementations of `ExchangeCapsule.Processor` depending on the `hardenedCalc` flag: [1](#0-0) 

When `hardenedCalc` is `false` (the default, unless the `AllowHardenExchangeCalculation` committee proposal has been activated), `ExchangeProcessor` is used: [2](#0-1) 

This is the exact analog of the reported RubiconMarket bug class:
- Line `long newBalance = balance + quant;` is raw, unchecked `long` addition instead of `StrictMathWrapper.addExact`/`Math.addExact` — it can silently wrap around on overflow instead of reverting.
- `double issuedSupply = -supply * (1.0 - Maths.pow(...))` and `double exchangeBalance = balance * (Maths.pow(...) - 1.0)` are raw floating point multiplications on potentially very large `long` values converted to `double`, losing precision and enabling incorrect results for large token amounts, exactly mirroring RubiconMarket's raw `*` multiplication against `1e18`/`1e9` constants that the auditors flagged as unsafe.

The codebase itself demonstrates awareness of this exact issue: `SafeExchangeProcessor` was added as the "hardened" replacement, using `BigDecimal` and `StrictMathWrapper.addExact` specifically to prevent silent overflow: [3](#0-2) 

and a dedicated test explicitly documents that the hardened path throws `ArithmeticException` on overflow while the default path does not perform this check: [4](#0-3) 

Because `AllowHardenExchangeCalculation` requires a witness-committee proposal to enable, the unsafe `ExchangeProcessor` remains the live, reachable default calculation path for every `ExchangeTransactionContract` submitted by any account, exercised directly from `ExchangeTransactionActuator.execute`: [5](#0-4) 

### Impact Explanation
If `firstTokenBalance + sellTokenQuant` (or the equivalent for the second token) overflows `Long.MAX_VALUE`, the raw `+` wraps to a negative value, corrupting the bonding-curve inputs. This can cause `buyTokenQuant` to be computed incorrectly (too large or too small), leading to improper fulfillment of trades against the on-chain TRC10/TRX exchange pool — analogous to RubiconMarket's improperly fulfilled purchase/sale amounts. Because `ExchangeTransactionActuator` then moves real TRX/TRC10 balances based on this value (`addAssetAmountV2`/`setBalance`), a corrupted result can misallocate real value between the trader and the exchange pool, i.e., theft of funds or permanent freezing/corruption of exchange pool balances.

### Likelihood Explanation
TRC10 tokens can be issued with attacker-chosen total supply up to `Long.MAX_VALUE` (63-bit range), and an exchange pair's balance can be built up over repeated `ExchangeInjectContract` calls. While `ExchangeInjectActuator` enforces `dynamicStore.getExchangeBalanceLimit()` on injections, `ExchangeTransactionActuator` (the actual trading path exercising `ExchangeProcessor`) does not itself validate against the same limit before performing the raw addition, so an attacker who can drive an exchange's token balance close to `Long.MAX_VALUE - sellTokenQuant` boundary through legitimate injects/trades could trigger overflow in `exchangeToSupply`. This requires the default (non-hardened) mode to still be active, which is the current default unless the community has already activated `AllowHardenExchangeCalculation`.

### Recommendation
Make `SafeExchangeProcessor` (or equivalent checked arithmetic) the unconditional path for `ExchangeCapsule.transaction()`, removing the unguarded `ExchangeProcessor` fallback, or retrofit `ExchangeProcessor.exchangeToSupply`/`exchangeFromSupply` to use `Math.addExact`/`BigInteger`/`BigDecimal` for all balance and multiplication operations, consistent with the `multiplyExact`/`BigInteger` approach already used in `ExchangeInjectActuator` and `ExchangeWithdrawActuator`.

### Proof of Concept
1. Issue a TRC10 token with total supply close to `Long.MAX_VALUE`.
2. Create an exchange pair and, through repeated `ExchangeInjectContract` calls (or direct large injections before the `ExchangeBalanceLimit` proposal is tightened), grow one side of the pool balance close to `Long.MAX_VALUE`.
3. Submit an `ExchangeTransactionContract` with a `quant` that pushes `firstTokenBalance + sellTokenQuant` (or `secondTokenBalance + sellTokenQuant`) past `Long.MAX_VALUE` in `ExchangeProcessor.exchangeToSupply` at [6](#0-5) , causing `newBalance` to wrap negative and corrupt the resulting `buyTokenQuant`, as demonstrated conceptually by the hardened-mode overflow test [7](#0-6)  (which only guards the hardened path, confirming the default path lacks equivalent protection).

### Citations

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-129)
```java
  public long transaction(byte[] sellTokenID, long sellTokenQuant, boolean useStrictMath,
      boolean hardenedCalc) throws ContractValidateException {
    long supply = 1_000_000_000_000_000_000L;
    Processor processor = hardenedCalc
        ? SafeExchangeProcessor.INSTANCE : new ExchangeProcessor(supply, useStrictMath);

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

**File:** chainbase/src/main/java/org/tron/core/capsule/SafeExchangeProcessor.java (L19-38)
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
```

**File:** framework/src/test/java/org/tron/core/actuator/ExchangeTransactionActuatorTest.java (L1872-1899)
```java
  /**
   * Hardened mode: corrupt pool with near-MAX balance triggers ArithmeticException
   * from addExact. Demonstrates the overflow-detection guard fires and is not
   * silently swallowed.
   */
  @Test
  public void hardenedExecuteOverflowThrowsArithmeticException() throws Exception {
    dbManager.getDynamicPropertiesStore().saveAllowSameTokenName(1);
    dbManager.getDynamicPropertiesStore().saveAllowHardenExchangeCalculation(1);
    InitExchangeSameTokenNameActive();

    long exchangeId = 1;
    // Corrupt pool to near-MAX TRX so addExact overflows when buying.
    ExchangeCapsule pool = dbManager.getExchangeV2Store().get(ByteArray.fromLong(exchangeId));
    pool.setBalance(Long.MAX_VALUE - 5L, 10_000_000L);
    dbManager.getExchangeV2Store().put(pool.createDbKey(), pool);

    String tokenId = "_";
    long quant = 100L;
    ExchangeTransactionActuator actuator = new ExchangeTransactionActuator();
    actuator.setChainBaseManager(dbManager.getChainBaseManager()).setAny(getContract(
        OWNER_ADDRESS_SECOND, exchangeId, tokenId, quant, 1));

    try {
      // addExact throws ArithmeticException, which is wrapped into ContractExeException.
      Assert.assertThrows(ContractExeException.class,
          () -> actuator.execute(new TransactionResultCapsule()));
    } finally {
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L64-69)
```java
      byte[] tokenID = exchangeTransactionContract.getTokenId().toByteArray();
      long tokenQuant = exchangeTransactionContract.getQuant();

      byte[] anotherTokenID;
      long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
          dynamicStore.allowStrictMath(), allowHarden());
```
