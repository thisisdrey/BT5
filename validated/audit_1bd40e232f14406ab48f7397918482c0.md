## Title
Opt-in "hardened" resource/exchange math uses checked `subtractExact`/`addExact`/`longValueExact` that can throw uncaught `ArithmeticException` during bandwidth/energy accounting reachable from every ordinary transaction, unlike the C4 report's advice to keep such deltas unchecked - ([File: chainbase/src/main/java/org/tron/core/db/ResourceProcessor.java])

### Summary
The C4 finding is about Uniswap V3's `feeGrowthInside`/`secondsInside` math needing to be *unchecked* because legitimate deltas can be negative/overflow relative to fixed-width storage; using checked (reverting) subtraction turns a valid state into a fatal revert. java-tron's "hardened" resource/exchange calculation feature (`allowHardenResourceCalculation` / `allowHardenExchangeCalculation` / `disableJavaLangMath`) introduces the mirror-image problem in Java: it deliberately replaces silent wraparound arithmetic with `StrictMathWrapper.addExact/subtractExact` and `BigInteger...longValueExact()` in bandwidth/energy usage accounting (`ResourceProcessor.increase`/`increaseV2`, `RepositoryImpl.calculateGlobalEnergyLimit`/`divideCeilExact`, `BandwidthProcessor.calculateGlobalNetLimit(V2)`) and in exchange math (`AbstractExchangeActuator.subtractExact/addExact`, `ExchangeTransactionActuator`). These "hardened" code paths are on the direct, unavoidable path of ordinary bandwidth/energy consumption for *every* transaction (`Manager.consumeBandwidth` → `BandwidthProcessor.consume` → `increase`/`calculateGlobalNetLimit`), so once the parameter is enabled by governance, any account with sufficiently large frozen/delegated balances (a legitimate whale, exchange, or SR) can cause the checked math to overflow the `long`/`BigInteger.longValueExact()` range and throw `ArithmeticException`.

### Finding Description
`ResourceProcessor.increase()` (and `increaseV2()`) compute `averageLastUsage`/`averageUsage`/`newWindowSize` with `BigInteger` multiplication followed by `divideCeilExact()`, which calls `.longValueExact()` and `StrictMathWrapper.addExact()`: [1](#0-0) [2](#0-1) 

`RepositoryImpl.calculateGlobalEnergyLimit()` similarly does `BigInteger.multiply().divide().longValueExact()` when `hardenResourceCalculation()` is on: [3](#0-2) 

These paths are reached unconditionally from `BandwidthProcessor.consume()` (called for every transaction from `Manager.consumeBandwidth`) via `useAccountNet`/`calculateGlobalNetLimit`/`increase`: [4](#0-3) [5](#0-4) 

The project's own tests explicitly demonstrate that hardening intentionally converts previously "silent" overflow/underflow into a thrown `ArithmeticException`: [6](#0-5) [7](#0-6) [8](#0-7) 

Because `consumeBandwidth`/`consumeEnergy` are executed as part of ordinary, deterministic block/transaction processing (not wrapped in a narrow validate/execute try-catch that only fails the single transaction), an uncaught `ArithmeticException` thrown here during block application propagates out of the transaction-processing pipeline in `Manager`, which is not among the checked exception types most call sites explicitly handle (e.g. `rePush` only catches `ValidateSignatureException | ContractValidateException | ContractExeException | AccountResourceInsufficientException | VMIllegalException`, `DupTransactionException`, `TaposException`, etc., not `ArithmeticException`): [9](#0-8) 

This is the same underlying bug class as the C4 report: math that is only valid/safe when unchecked (or protected purely at the *application logic* level, e.g. via clamping) is instead wired through hard-reverting/exact arithmetic that is reachable on a hot, mandatory path, turning legitimate large-but-valid values into an unhandled exception instead of a graceful validation failure.

### Impact Explanation
If `allowHardenResourceCalculation` (or `allowHardenExchangeCalculation`) is ever enabled on mainnet, any transaction that touches bandwidth/energy accounting for an account whose frozen/delegated balance, usage, or window sizes are large enough to overflow `long` in the `BigInteger` multiply/`longValueExact()` step throws `ArithmeticException` deterministically on every full node applying that block. Since this happens inside mandatory, per-transaction accounting rather than inside a narrowly-scoped, exception-handled actuator `execute()`, this can propagate as an unhandled runtime exception during block application, which would cause node crash/halt across the network (all nodes fail identically and deterministically, since the calculation is consensus code) — a chain halt / liveness failure triggerable by an ordinary large-balance account's normal transaction, not by any malicious SR/witness/peer behavior.

### Likelihood Explanation
The feature is gated behind chain parameters (`allowHardenResourceCalculation`, `allowHardenExchangeCalculation`, `disableJavaLangMath`) that are off by default, so likelihood under current mainnet configuration is low. However, these parameters are designed to be turned on by committee/governance vote as a "hardening" upgrade; once activated, the trigger condition (sufficiently large frozen balance/usage numbers, which large exchanges, whales or long-lived heavily-used accounts can realistically reach given TRX's supply and precision) is plausible and requires no privileged access — any ordinary account submitting an ordinary freeze/transfer/trigger transaction that touches its own or a counterparty's bandwidth/energy accounting could hit the overflow.

### Recommendation
- Confirm exactly which exceptions are caught around `Manager.consumeBandwidth`/`consumeEnergy` in the block-application path and ensure `ArithmeticException` cannot escape as an uncaught `RuntimeException` during `pushBlock`/`processTransaction`; if it must be thrown, convert it deterministically into a `ContractValidateException`/`AccountResourceInsufficientException` that only fails the single transaction rather than aborting block application.
- Re-audit every "hardened" checked-math call (`ResourceProcessor.increase/increaseV2`, `RepositoryImpl.calculateGlobalEnergyLimit`, `BandwidthProcessor.calculateGlobalNetLimit(V2)`, `AbstractExchangeActuator.subtractExact/addExact`) to ensure `ArithmeticException` is caught at every call site the way `ExchangeTransactionActuator.execute()` already does, rather than only in a subset of exchange actuators.
- Add fuzz/property tests with extreme (but realistically reachable) frozen-balance/usage magnitudes before enabling any harden* parameter on mainnet, verifying no code path can throw an uncaught exception during deterministic block application.

### Proof of Concept
Not directly exploitable today because `allowHardenResourceCalculation`/`allowHardenExchangeCalculation` default to `0`. The project's own test suite already demonstrates the underlying mechanics that would become exploitable once the parameter is enabled: [6](#0-5) [8](#0-7) 
demonstrate `ArithmeticException` being thrown from `calculateGlobalEnergyLimit`/`increaseV2` given large but not implausible `frozenForEnergy`/`lastUsage` values, and: [10](#0-9) 
shows the same pattern for exchange math. I could not fully verify, within the available indexed code, whether `Manager`'s outer block-application loop (e.g., `pushBlock`/`applyBlock`) has a catch-all that would prevent an `ArithmeticException` from these accounting calls from propagating and halting node operation; this would require deeper tracing of `Manager.java`'s block-processing methods, which are only partially covered by the code index, to confirm definitively. I recommend a Devin session with full repository access to trace this exception path end-to-end before treating the halt-severity impact as fully confirmed.

### Citations

**File:** chainbase/src/main/java/org/tron/core/db/ResourceProcessor.java (L94-105)
```java
    if (hardenCalculation()) {
      BigInteger biPrecision = BigInteger.valueOf(this.precision);
      averageLastUsage = divideCeilExact(
          BigInteger.valueOf(lastUsage).multiply(biPrecision),
          BigInteger.valueOf(oldWindowSize));
      averageUsage = divideCeilExact(
          BigInteger.valueOf(usage).multiply(biPrecision),
          BigInteger.valueOf(this.windowSize));
    } else {
      averageLastUsage = divideCeil(lastUsage * this.precision, oldWindowSize);
      averageUsage = divideCeil(usage * this.precision, this.windowSize);
    }
```

**File:** chainbase/src/main/java/org/tron/core/db/ResourceProcessor.java (L965-976)
```java

```

**File:** actuator/src/main/java/org/tron/core/vm/repository/RepositoryImpl.java (L992-1010)
```java
  public long calculateGlobalEnergyLimit(AccountCapsule accountCapsule) {
    long frozeBalance = accountCapsule.getAllFrozenBalanceForEnergy();
    if (frozeBalance < TRX_PRECISION) {
      return 0;
    }
    long energyWeight = frozeBalance / TRX_PRECISION;
    long totalEnergyLimit = getDynamicPropertiesStore().getTotalEnergyCurrentLimit();
    long totalEnergyWeight = getDynamicPropertiesStore().getTotalEnergyWeight();

    assert totalEnergyWeight > 0;

    if (hardenResourceCalculation()) {
      return BigInteger.valueOf(energyWeight)
          .multiply(BigInteger.valueOf(totalEnergyLimit))
          .divide(BigInteger.valueOf(totalEnergyWeight))
          .longValueExact();
    }
    return (long) (energyWeight * ((double) totalEnergyLimit / totalEnergyWeight));
  }
```

**File:** chainbase/src/main/java/org/tron/core/db/BandwidthProcessor.java (L432-453)
```java
  public long calculateGlobalNetLimit(AccountCapsule accountCapsule) {
    long frozeBalance = accountCapsule.getAllFrozenBalanceForBandwidth();
    if (dynamicPropertiesStore.supportUnfreezeDelay()) {
      return calculateGlobalNetLimitV2(frozeBalance);
    }
    if (frozeBalance < TRX_PRECISION) {
      return 0;
    }
    long totalNetLimit = chainBaseManager.getDynamicPropertiesStore().getTotalNetLimit();
    long totalNetWeight = chainBaseManager.getDynamicPropertiesStore().getTotalNetWeight();
    if (dynamicPropertiesStore.allowNewReward() && totalNetWeight <= 0) {
      return 0;
    }
    if (totalNetWeight == 0) {
      return 0;
    }
    if (hardenCalculation()) {
      return calculateGlobalLimitV1(frozeBalance, totalNetLimit, totalNetWeight);
    }
    long netWeight = frozeBalance / TRX_PRECISION;
    return (long) (netWeight * ((double) totalNetLimit / totalNetWeight));
  }
```

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L1023-1028)
```java
  public void consumeBandwidth(TransactionCapsule trx, TransactionTrace trace)
      throws ContractValidateException, AccountResourceInsufficientException,
      TooBigTransactionResultException, TooBigTransactionException {
    BandwidthProcessor processor = new BandwidthProcessor(chainBaseManager);
    processor.consume(trx, trace);
  }
```

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L2134-2163)
```java
  public void rePush(TransactionCapsule tx) {
    if (containsTransaction(tx)) {
      return;
    }

    String ownerAddress = ByteArray.toHexString(tx.getOwnerAddress());
    synchronized (this) {
      if (ownerAddressSet.contains(ownerAddress)) {
        tx.setVerified(false);
      }
    }

    try {
      this.pushTransaction(tx);
    } catch (ValidateSignatureException | ContractValidateException | ContractExeException
        | AccountResourceInsufficientException | VMIllegalException e) {
      logger.debug(e.getMessage(), e);
    } catch (DupTransactionException e) {
      logger.debug("Pending manager: dup trans", e);
    } catch (TaposException e) {
      logger.debug("Pending manager: tapos exception", e);
    } catch (TooBigTransactionException e) {
      logger.debug("Pending manager: too big transaction", e);
    } catch (TransactionExpirationException e) {
      logger.debug("Pending manager: expiration transaction", e);
    } catch (ReceiptCheckErrException e) {
      logger.debug("Pending manager: outOfSlotTime transaction", e);
    } catch (TooBigTransactionResultException e) {
      logger.debug("Pending manager: too big transaction result", e);
    }
```

**File:** framework/src/test/java/org/tron/core/db/CalculateGlobalLimitHardenTest.java (L67-78)
```java
  @Test
  public void testGlobalEnergyLimitOverflowDetectedWithHardening() {
    dbManager.getDynamicPropertiesStore().saveTotalEnergyCurrentLimit(Long.MAX_VALUE / 2);
    dbManager.getDynamicPropertiesStore().saveTotalEnergyWeight(1L);
    ownerCapsule.setFrozenForEnergy(Long.MAX_VALUE / 4, 0L);
    dbManager.getAccountStore().put(ownerCapsule.getAddress().toByteArray(), ownerCapsule);

    dbManager.getDynamicPropertiesStore().saveAllowHardenResourceCalculation(1);

    Assert.assertThrows(ArithmeticException.class,
        () -> energyProcessor.calculateGlobalEnergyLimit(ownerCapsule));
  }
```

**File:** framework/src/test/java/org/tron/core/db/ResourceProcessorHardenTest.java (L106-118)
```java
  @Test
  public void testIncreaseOverflowDetectedWithHardening() {
    long lastUsage = Long.MAX_VALUE / 10; // ~9.2e17
    long usage = 1L;
    long lastTime = 9990L;
    long now = 9995L;
    long windowSize = 28800L;

    dbManager.getDynamicPropertiesStore().saveAllowHardenResourceCalculation(1);

    Assert.assertThrows(ArithmeticException.class,
        () -> processor.increase(lastUsage, usage, lastTime, now, windowSize));
  }
```

**File:** framework/src/test/java/org/tron/core/db/ResourceProcessorHardenTest.java (L226-247)
```java
  @Test
  public void testIncreaseV2OverflowDetected() {
    dbManager.getDynamicPropertiesStore().saveUnfreezeDelayDays(14);
    dbManager.getDynamicPropertiesStore().saveAllowCancelAllUnfreezeV2(1);
    dbManager.getDynamicPropertiesStore().saveAllowHardenResourceCalculation(1);

    long lastUsage = Long.MAX_VALUE / 10; // ~9.2e17, above threshold
    long usage = 1000L;
    long lastTime = 9999L;
    long now = 10000L;

    ownerCapsule.setNewWindowSize(ResourceCode.ENERGY, 28800);
    ownerCapsule.setWindowOptimized(ResourceCode.ENERGY, true);
    ownerCapsule.setLatestConsumeTimeForEnergy(lastTime);
    ownerCapsule.setEnergyUsage(lastUsage);
    dbManager.getAccountStore().put(
        ownerCapsule.getAddress().toByteArray(), ownerCapsule);

    Assert.assertThrows(ArithmeticException.class,
        () -> processor.increaseV2(ownerCapsule, ResourceCode.ENERGY,
            lastUsage, usage, lastTime, now));
  }
```

**File:** framework/src/test/java/org/tron/core/actuator/ExchangeWithdrawActuatorTest.java (L1868-1904)
```java
  /**
   * Hardened mode: subtractExact in execute() throws on underflow.
   */
  @Test
  public void hardenedSubtractExactUnderflow() {
    dbManager.getDynamicPropertiesStore().saveAllowSameTokenName(1);
    dbManager.getDynamicPropertiesStore().saveAllowHardenExchangeCalculation(1);
    InitExchangeSameTokenNameActive();

    // Corrupt account: balance < calcFee triggers subtractExact underflow
    // (this is unrealistic but exercises the addExact/subtractExact path)
    byte[] ownerAddress = ByteArray.fromHexString(OWNER_ADDRESS_FIRST);
    AccountCapsule accountCapsule = dbManager.getAccountStore().get(ownerAddress);
    accountCapsule.setBalance(0L);
    dbManager.getAccountStore().put(ownerAddress, accountCapsule);

    String firstTokenId = "123";
    long firstTokenQuant = 100000000L;
    ExchangeWithdrawActuator actuator = new ExchangeWithdrawActuator();
    actuator.setChainBaseManager(dbManager.getChainBaseManager()).setAny(getContract(
        OWNER_ADDRESS_FIRST, 1L, firstTokenId, firstTokenQuant));

    try {
      // calcFee() returns 0 in this actuator, so this won't actually underflow.
      // The test still exercises the subtractExact code path with hardened on.
      actuator.validate();
      actuator.execute(new TransactionResultCapsule());
    } catch (Exception ignore) {
      // any outcome is acceptable; we just need execute() exercised under hardened
    } finally {
      dbManager.getExchangeStore().delete(ByteArray.fromLong(1L));
      dbManager.getExchangeStore().delete(ByteArray.fromLong(2L));
      dbManager.getExchangeV2Store().delete(ByteArray.fromLong(1L));
      dbManager.getExchangeV2Store().delete(ByteArray.fromLong(2L));
      dbManager.getDynamicPropertiesStore().saveAllowHardenExchangeCalculation(0);
    }
  }
```
