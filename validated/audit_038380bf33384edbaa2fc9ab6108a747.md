### Title
Exchange balance overflow when `ALLOW_HARDEN_EXCHANGE_CALCULATION` is disabled - (File: `chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java`)

### Summary
CVE-2022-21556 is a MySQL *Optimizer* bug that lets a network-reachable actor corrupt data or crash the server through miscomputed query execution. The closest reachable analog in java-tron is in the Bancor-style exchange/order arithmetic: `ExchangeCapsule.transaction()` performs the balance update with raw, unchecked `long` addition/subtraction (`firstTokenBalance + sellTokenQuant`, `secondTokenBalance - buyTokenQuant`, etc.) whenever the committee-gated `allowHardenExchangeCalculation` flag is not enabled, instead of always going through the overflow-checked `SafeExchangeProcessor`/`StrictMathWrapper` path.

### Finding Description
`ExchangeCapsule.transaction(byte[] sellTokenID, long sellTokenQuant, boolean useStrictMath, boolean hardenedCalc)` selects the arithmetic mode based on `hardenedCalc`: [1](#0-0) 

When `hardenedCalc` is `false` (i.e. the committee proposal `allowHardenExchangeCalculation` has not been activated, which is the default/disabled state on any chain until SRs pass that proposal), the new balances are computed with plain Java `+`/`-` on `long`, with **no overflow check**: [2](#0-1) 

The `allowHarden()` gate is defined in `AbstractExchangeActuator`, and both `addExact`/`subtractExact` helpers used elsewhere in the actuator also fall back to unchecked `x + y` / `x - y` when the flag is off: [3](#0-2) 

This code path is directly reachable by any unprivileged account through `ExchangeTransactionContract` (`ExchangeTransactionActuator.execute`/`doValidate`, which calls `exchangeCapsule.transaction(tokenID, tokenQuant, dynamicStore.allowStrictMath(), allowHarden())`): [4](#0-3) 
and through `ExchangeInjectActuator`, which independently reimplements the same unchecked `BigInteger`/`long` mixing for asset ratio computation: [5](#0-4) 

The legacy `ExchangeProcessor` (non-hardened) internal Bancor formula also uses raw addition for `balance + quant` and casts a `double` result to `long`, both of which can silently wrap or truncate for large balances: [6](#0-5) 

The repository's own hardened test suite explicitly demonstrates that the safe path throws `ArithmeticException` on overflow (`hardenedExecuteOverflowThrowsArithmeticException`, `testHardenedOverflowDetection`), confirming that the unhardened default path does **not** perform this validation: [7](#0-6) [8](#0-7) 

The only barrier preventing the balances from ever getting close to overflow is `getExchangeBalanceLimit()`, a dynamic (committee-settable) property checked in `doValidate()`: [9](#0-8) 
If this limit is set (or left) close to `Long.MAX_VALUE`, or an issuer creates a token with very large `total_supply` and repeatedly injects/sells into a pair they created, the pool balances can be driven near the 64-bit boundary, after which a single further `ExchangeTransactionContract` from an unprivileged account triggers wraparound of `firstTokenBalance`/`secondTokenBalance`, corrupting the pool's stored state (negative or bogus balances persisted via `Commons.putExchangeCapsule`).

### Impact Explanation
A corrupted/overflowed exchange balance is persisted directly into consensus state (`ExchangeStore`/`ExchangeV2Store`), so it becomes part of every full node's ledger. Depending on the sign of the wraparound, this can (a) let subsequent traders receive far more tokens than the pool actually backs (unbacked balance / theft of the counter-asset), or (b) freeze the pool permanently by making balances negative/invalid so later actuator calls throw and no user can trade against it. This matches the CVE's Integrity/Availability impact pattern (data corruption / hang), adapted to an unprivileged, permissionlessly reachable path.

### Likelihood Explanation
Reaching a true overflow requires the pool's `firstTokenBalance`/`secondTokenBalance` to be near `Long.MAX_VALUE`, which is gated by the dynamic `exchangeBalanceLimit` property and by how much of a self-issued TRC10 asset an attacker can accumulate. This makes the attack **conditional on network configuration** (a permissive `exchangeBalanceLimit` and `allowHardenExchangeCalculation` still disabled), rather than trivially exploitable on every deployment today. On networks/testnets where the hardening proposal has not yet been activated and balance limits are generous, the bug is directly reachable by any account issuing a token and using the public `ExchangeCreate`/`ExchangeInject`/`ExchangeTransaction` contracts — no special privilege is required.

### Recommendation
- Make the overflow-checked path (`SafeExchangeProcessor`, `StrictMathWrapper.addExact/subtractExact`) unconditional in `ExchangeCapsule.transaction()` and `AbstractExchangeActuator.addExact/subtractExact`, rather than gating it behind the `allowHardenExchangeCalculation` committee proposal.
- Apply the same fix to `ExchangeInjectActuator.doValidate()`'s `BigInteger`→`longValueExact()` ratio computation to ensure it always throws rather than silently truncating.
- Independently of the committee flag, enforce a hard ceiling in code (not just a settable dynamic property) on exchange pool balances well below `Long.MAX_VALUE / 2` to eliminate any overflow headroom.

### Proof of Concept
1. Deploy/target a network where `allowHardenExchangeCalculation` is still `0` (default/unactivated) and `exchangeBalanceLimit` is set high.
2. Issue a TRC10 asset with maximal `total_supply` via `AssetIssueContract`.
3. Create an exchange pair with this asset via `ExchangeCreateContract`, then repeatedly call `ExchangeInjectContract` to push `firstTokenBalance`/`secondTokenBalance` close to `Long.MAX_VALUE`.
4. Submit an `ExchangeTransactionContract` with a `quant` chosen so that `firstTokenBalance + sellTokenQuant` (in `ExchangeCapsule.transaction`, non-hardened branch) exceeds `Long.MAX_VALUE`, wrapping to a negative/garbage value that gets persisted via `Commons.putExchangeCapsule`.
5. Observe the corrupted exchange balance in `ExchangeStore`/`ExchangeV2Store`, and subsequent trades against the pool either yielding unbacked token amounts or failing permanently.

### Citations

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-158)
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L61-69)
```java
      byte[] firstTokenID = exchangeCapsule.getFirstTokenId();
      byte[] secondTokenID = exchangeCapsule.getSecondTokenId();

      byte[] tokenID = exchangeTransactionContract.getTokenId().toByteArray();
      long tokenQuant = exchangeTransactionContract.getQuant();

      byte[] anotherTokenID;
      long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
          dynamicStore.allowStrictMath(), allowHarden());
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L199-205)
```java
    long balanceLimit = dynamicStore.getExchangeBalanceLimit();
    long tokenBalance = (Arrays.equals(tokenID, firstTokenID) ? firstTokenBalance
        : secondTokenBalance);
    tokenBalance = addExact(tokenBalance, tokenQuant);
    if (tokenBalance > balanceLimit) {
      throw new ContractValidateException("token balance must less than " + balanceLimit);
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L215-227)
```java
    if (Arrays.equals(tokenID, firstTokenID)) {
      anotherTokenID = secondTokenID;
      anotherTokenQuant = bigSecondTokenBalance.multiply(bigTokenQuant)
          .divide(bigFirstTokenBalance).longValueExact();
      newTokenBalance = addExact(firstTokenBalance, tokenQuant);
      newAnotherTokenBalance = addExact(secondTokenBalance, anotherTokenQuant);
    } else {
      anotherTokenID = firstTokenID;
      anotherTokenQuant = bigFirstTokenBalance.multiply(bigTokenQuant)
          .divide(bigSecondTokenBalance).longValueExact();
      newTokenBalance = addExact(secondTokenBalance, tokenQuant);
      newAnotherTokenBalance = addExact(firstTokenBalance, anotherTokenQuant);
    }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java (L17-29)
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
```

**File:** framework/src/test/java/org/tron/core/capsule/utils/ExchangeProcessorTest.java (L159-163)
```java
  @Test
  public void testHardenedOverflowDetection() {
    assertThrows(ArithmeticException.class, () ->
        SafeExchangeProcessor.INSTANCE.exchange(Long.MAX_VALUE, 1_000_000L, 1L));
  }
```

**File:** framework/src/test/java/org/tron/core/actuator/ExchangeTransactionActuatorTest.java (L1877-1905)
```java
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
      dbManager.getExchangeStore().delete(ByteArray.fromLong(1L));
      dbManager.getExchangeStore().delete(ByteArray.fromLong(2L));
      dbManager.getExchangeV2Store().delete(ByteArray.fromLong(1L));
      dbManager.getExchangeV2Store().delete(ByteArray.fromLong(2L));
      dbManager.getDynamicPropertiesStore().saveAllowHardenExchangeCalculation(0);
    }
```
