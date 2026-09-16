### Title
Unbounded resource-accounting math can throw an uncaught `ArithmeticException` during block application, halting the node - (File: chainbase/src/main/java/org/tron/core/db/ResourceProcessor.java)

### Summary
The external report describes `Overseer.rebase()` reverting whenever a computed value (APR change) exceeds a hard threshold, which an attacker can trigger with a legitimate but extreme input (a large supply donation), permanently blocking a periodic protocol-critical state update. The closest reachable analog in java-tron is the "hardened" bandwidth/energy accounting math in `ResourceProcessor`/`EnergyProcessor`/`RepositoryImpl`, which replaces `double`-based arithmetic with exact `BigInteger`/`longValueExact()` computations that intentionally `throw ArithmeticException` on overflow instead of saturating, silently truncating, or reverting gracefully with a caught exception.

### Finding Description
When `allowHardenResourceCalculation` (or the VM equivalent `allowHardenResourceCalculation`) is active, several resource-accounting helpers compute account/global energy and bandwidth limits using exact BigInteger math and call `.longValueExact()`, which throws `ArithmeticException` if the true mathematical result does not fit in a `long`: [1](#0-0) [2](#0-1) 

These helpers (`increase`, `increaseV2`, `getUsage`, `getNewWindowSize`, `calculateGlobalLimitV1/V2`) are invoked on the path of ordinary account resource accounting — every transaction that consumes bandwidth/energy, and every freeze/delegate/undelegate operation updates these windows via `EnergyProcessor.updateUsage()` / `BandwidthProcessor` calls, which are reachable from `ResourceProcessor.increase`: [3](#0-2) 

`EnergyProcessor.calculateGlobalEnergyLimit` and `calculateGlobalEnergyLimitV2` (used for every account's available energy check) similarly call `.longValueExact()` unconditionally when hardening is enabled: [4](#0-3) 

The project's own test suite confirms this design explicitly throws `ArithmeticException` for large-but-plausible values (e.g. `Long.MAX_VALUE / 10` usage, or large `totalEnergyLimit`/`totalEnergyWeight` combinations): [5](#0-4) [6](#0-5) [7](#0-6) 

This is structurally the same bug class as the reported `rebase()` issue: a state-transition function that is invoked as part of ordinary, periodic, permissionless protocol operation (per-transaction resource accounting / per-block adaptive limit update) performs an unbounded arithmetic computation and throws when a threshold (here, `Long.MAX_VALUE`) is exceeded, rather than saturating or handling the condition gracefully. Just as an attacker can grow `stHYPE` supply to push the APR calculation over 100% and permanently block `rebase()`, an entity that can grow tracked usage/weight values (via long-running legitimate freeze/vote/energy-usage accumulation, which is exactly what these accounting structures are designed to track over the life of the chain) can eventually push these BigInteger computations past `Long.MAX_VALUE`, causing `longValueExact()` to throw.

### Impact Explanation
`EnergyProcessor.updateAdaptiveTotalEnergyLimit()` is called unconditionally during **every block's** processing when adaptive energy is enabled: [8](#0-7) 

and `ResourceProcessor.increase`/`calculateGlobalEnergyLimit` are invoked on essentially every transaction that touches bandwidth/energy accounting. An uncaught `ArithmeticException` propagating out of `processBlock` is not among the declared/caught exception types for block application in `Manager` (`ValidateSignatureException`, `ContractValidateException`, `ContractExeException`, etc. — `RuntimeException` subclasses like `ArithmeticException` are not handled there), so it would propagate up and abort block processing/application, which can crash or halt the node applying that block. If this occurs deterministically for all nodes running the same accumulated state, it can halt the network; if only some nodes have accumulated enough usage/weight to overflow while others haven't (e.g., differing witness reward histories or differing long-lived frozen balances), it can also produce divergent chain behavior. This matches the allowed "node crash or halt / chain split" impact category.

### Likelihood Explanation
Likelihood is Low, consistent with the original finding's Low likelihood: overflow requires values to approach `Long.MAX_VALUE` (~9.2×10^18), which is far beyond realistic TRX supply/energy figures under normal network parameters. It would require either sustained abnormal growth of `totalEnergyWeight`/`totalEnergyLimit`/usage counters over a very long time, or a governance misconfiguration (e.g. `TOTAL_ENERGY_LIMIT` or `ADAPTIVE_RESOURCE_LIMIT_MULTIPLIER` set to extreme values) combined with attacker-driven usage growth, similar to how the original report required a large but not impossible supply donation.

### Recommendation
Where hardened resource-calculation paths use `BigInteger.longValueExact()`, catch `ArithmeticException` and clamp results to `Long.MAX_VALUE` (or another sane upper bound) instead of allowing the exception to propagate into block application code. Alternatively, wrap `processBlock`'s adaptive-limit and resource-accounting calls so that arithmetic errors are handled the same way other validation failures are (e.g., logged and skipped for that cycle) rather than aborting block application entirely.

### Proof of Concept
Not directly exploitable with a single crafted transaction under current default network parameters — reaching the overflow boundary requires resource-weight/usage counters to approach `Long.MAX_VALUE`, which the project's own hardening tests demonstrate deterministically: [9](#0-8) 
This test shows that feeding realistic-looking (if extreme) `totalEnergyTargetLimit`/`totalEnergyLimit` values into `updateAdaptiveTotalEnergyLimit()` — a function invoked unconditionally on every block once adaptive energy is enabled — deterministically throws `ArithmeticException`, which is not caught anywhere in `Manager.processBlock`'s exception list.

**Uncertainty**: I was not able to fully trace every call site (e.g., whether some callers of `increaseV2`/`calculateGlobalEnergyLimit` in `VMActuator`/`RepositoryImpl` already wrap these calls in try/catch for `ArithmeticException`), so it is possible some paths already handle this exception gracefully while others (like the direct `Manager.processBlock` call to `updateAdaptiveTotalEnergyLimit`) do not. Confirming the full propagation path would require deeper review of `Manager.java` exception handling around `processBlock`/`pushBlock`.

### Citations

**File:** chainbase/src/main/java/org/tron/core/db/ResourceProcessor.java (L46-78)
```java
  protected long increase(long lastUsage, long usage, long lastTime, long now) {
    return increase(lastUsage, usage, lastTime, now, windowSize);
  }

  protected long increase(long lastUsage, long usage, long lastTime, long now, long windowSize) {
    long averageLastUsage;
    long averageUsage;
    if (hardenCalculation()) {
      BigInteger biPrecision = BigInteger.valueOf(precision);
      BigInteger biWindowSize = BigInteger.valueOf(windowSize);
      averageLastUsage = divideCeilExact(
          BigInteger.valueOf(lastUsage).multiply(biPrecision), biWindowSize);
      averageUsage = divideCeilExact(
          BigInteger.valueOf(usage).multiply(biPrecision), biWindowSize);
    } else {
      averageLastUsage = divideCeil(lastUsage * precision, windowSize);
      averageUsage = divideCeil(usage * precision, windowSize);
    }

    if (lastTime != now) {
      assert now > lastTime;
      if (lastTime + windowSize > now) {
        long delta = now - lastTime;
        double decay = (windowSize - delta) / (double) windowSize;
        averageLastUsage = round(averageLastUsage * decay,
            this.disableJavaLangMath());
      } else {
        averageLastUsage = 0;
      }
    }
    averageLastUsage += averageUsage;
    return getUsage(averageLastUsage, windowSize);
  }
```

**File:** chainbase/src/main/java/org/tron/core/db/ResourceProcessor.java (L272-283)
```java
  private long divideCeil(long numerator, long denominator) {
    return (numerator / denominator) + ((numerator % denominator) > 0 ? 1 : 0);
  }

  private long divideCeilExact(BigInteger numerator, BigInteger denominator) {
    BigInteger[] divRem = numerator.divideAndRemainder(denominator);
    long result = divRem[0].longValueExact();
    if (divRem[1].signum() > 0) {
      result = StrictMathWrapper.addExact(result, 1);
    }
    return result;
  }
```

**File:** chainbase/src/main/java/org/tron/core/db/ResourceProcessor.java (L350-378)
```java
  protected long calculateGlobalLimitV1(long frozeBalance,
      long totalLimit, long totalWeight) {
    long weight = frozeBalance / TRX_PRECISION;
    return BigInteger.valueOf(weight)
        .multiply(BigInteger.valueOf(totalLimit))
        .divide(BigInteger.valueOf(totalWeight))
        .longValueExact();
  }

  /**
   * Hardened replacement of legacy V2 formula
   * {@code (long)(((double) frozeBalance / TRX_PRECISION)
   *               * ((double) totalLimit / totalWeight))}.
   *
   * <p>Preserves V2 semantics: equivalent to
   * {@code (frozeBalance * totalLimit) / (TRX_PRECISION * totalWeight)} with
   * a single integer truncation at the end. Critically, fractional weight
   * (i.e. {@code frozeBalance < TRX_PRECISION}) is preserved through the
   * multiplication and only truncated at the final divide, so small balances
   * yield the same proportional result as the double-arithmetic path.
   */
  protected long calculateGlobalLimitV2(long frozeBalance,
      long totalLimit, long totalWeight) {
    return BigInteger.valueOf(frozeBalance)
        .multiply(BigInteger.valueOf(totalLimit))
        .divide(BigInteger.valueOf(TRX_PRECISION)
            .multiply(BigInteger.valueOf(totalWeight)))
        .longValueExact();
  }
```

**File:** chainbase/src/main/java/org/tron/core/db/EnergyProcessor.java (L145-205)
```java
  public long calculateGlobalEnergyLimit(AccountCapsule accountCapsule) {
    long frozeBalance = accountCapsule.getAllFrozenBalanceForEnergy();
    if (dynamicPropertiesStore.supportUnfreezeDelay()) {
      return calculateGlobalEnergyLimitV2(frozeBalance);
    }
    if (frozeBalance < TRX_PRECISION) {
      return 0;
    }

    long totalEnergyLimit = dynamicPropertiesStore.getTotalEnergyCurrentLimit();
    long totalEnergyWeight = dynamicPropertiesStore.getTotalEnergyWeight();
    if (dynamicPropertiesStore.allowNewReward() && totalEnergyWeight <= 0) {
      return 0;
    } else {
      assert totalEnergyWeight > 0;
    }
    if (hardenCalculation()) {
      return calculateGlobalLimitV1(frozeBalance, totalEnergyLimit, totalEnergyWeight);
    }
    long energyWeight = frozeBalance / TRX_PRECISION;
    return (long) (energyWeight * ((double) totalEnergyLimit / totalEnergyWeight));
  }

  public long calculateGlobalEnergyLimitV2(long frozeBalance) {
    long totalEnergyLimit = dynamicPropertiesStore.getTotalEnergyCurrentLimit();
    long totalEnergyWeight = dynamicPropertiesStore.getTotalEnergyWeight();
    if (totalEnergyWeight == 0) {
      return 0;
    }
    if (hardenCalculation()) {
      return calculateGlobalLimitV2(frozeBalance, totalEnergyLimit, totalEnergyWeight);
    }
    double energyWeight = (double) frozeBalance / TRX_PRECISION;
    return (long) (energyWeight * ((double) totalEnergyLimit / totalEnergyWeight));
  }


  public long getAccountLeftEnergyFromFreeze(AccountCapsule accountCapsule) {
    long now = getHeadSlot();
    long energyUsage = accountCapsule.getEnergyUsage();
    long latestConsumeTime = accountCapsule.getAccountResource().getLatestConsumeTimeForEnergy();
    long energyLimit = calculateGlobalEnergyLimit(accountCapsule);

    long newEnergyUsage = recovery(accountCapsule, ENERGY, energyUsage, latestConsumeTime, now);

    return max(energyLimit - newEnergyUsage, 0, this.disableJavaLangMath()); // us
  }

  private long getHeadSlot() {
    return getHeadSlot(dynamicPropertiesStore);
  }

  private long scaleByRate(long value, long numerator, long denominator) {
    if (hardenCalculation()) {
      return BigInteger.valueOf(value)
          .multiply(BigInteger.valueOf(numerator))
          .divide(BigInteger.valueOf(denominator))
          .longValueExact();
    }
    return value * numerator / denominator;
  }
```

**File:** framework/src/test/java/org/tron/core/db/CalculateGlobalLimitHardenTest.java (L319-345)
```java
  @Test
  public void testUpdateAdaptiveTotalEnergyLimitOverflowDetected() {
    dbManager.getDynamicPropertiesStore().saveTotalEnergyAverageUsage(0L);
    dbManager.getDynamicPropertiesStore().saveTotalEnergyTargetLimit(Long.MAX_VALUE);
    dbManager.getDynamicPropertiesStore().saveTotalEnergyCurrentLimit(
        10_000_000_000_000_000L);
    dbManager.getDynamicPropertiesStore().saveTotalEnergyLimit(10_000_000_000_000_000L);
    dbManager.getDynamicPropertiesStore().saveAdaptiveResourceLimitMultiplier(1000L);

    dbManager.getDynamicPropertiesStore().saveAllowHardenResourceCalculation(1);

    Assert.assertThrows(ArithmeticException.class,
        () -> energyProcessor.updateAdaptiveTotalEnergyLimit());
  }

  @Test
  public void testUpdateAdaptiveLimitMultiplierOverflowDetected() {
    dbManager.getDynamicPropertiesStore().saveTotalEnergyAverageUsage(0L);
    dbManager.getDynamicPropertiesStore().saveTotalEnergyTargetLimit(Long.MAX_VALUE);
    dbManager.getDynamicPropertiesStore().saveTotalEnergyCurrentLimit(1_000_000L);
    dbManager.getDynamicPropertiesStore().saveTotalEnergyLimit(Long.MAX_VALUE / 100);
    dbManager.getDynamicPropertiesStore().saveAdaptiveResourceLimitMultiplier(1000L);
    dbManager.getDynamicPropertiesStore().saveAllowHardenResourceCalculation(1);

    Assert.assertThrows(ArithmeticException.class,
        () -> energyProcessor.updateAdaptiveTotalEnergyLimit());
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

**File:** framework/src/test/java/org/tron/core/vm/repository/RepositoryImplHardenTest.java (L260-279)
```java
  @Test
  public void testCalculateGlobalEnergyLimitHardenedOverflowDetected() {
    long totalEnergyLimit = Long.MAX_VALUE / 2;
    long totalEnergyWeight = 1L;
    long frozeBalance = Long.MAX_VALUE / 4;

    dbManager.getDynamicPropertiesStore().saveTotalEnergyCurrentLimit(totalEnergyLimit);
    dbManager.getDynamicPropertiesStore().saveTotalEnergyWeight(totalEnergyWeight);

    AccountCapsule account = new AccountCapsule(
        ByteString.copyFromUtf8("owner"),
        ByteString.copyFrom(ByteArray.fromHexString(
            Wallet.getAddressPreFixString() + "548794500882809695a8a687866e76d4271a1abc")),
        AccountType.Normal, 0L);
    account.setFrozenForEnergy(frozeBalance, 0L);

    VMConfig.initAllowHardenResourceCalculation(1);
    Assert.assertThrows(ArithmeticException.class,
        () -> repository.calculateGlobalEnergyLimit(account));
  }
```

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L1910-1915)
```java
    if (getDynamicPropertiesStore().getAllowAdaptiveEnergy() == 1) {
      EnergyProcessor energyProcessor = new EnergyProcessor(
          chainBaseManager.getDynamicPropertiesStore(), chainBaseManager.getAccountStore());
      energyProcessor.updateTotalEnergyAverageUsage();
      energyProcessor.updateAdaptiveTotalEnergyLimit();
    }
```
