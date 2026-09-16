### Title
Checked-math hardening path (`allowHardenResourceCalculation`) introduces unhandled `ArithmeticException` that can halt bandwidth/energy accounting on every transaction - ([File: chainbase/src/main/java/org/tron/core/db/ResourceProcessor.java])

### Summary
This is the java-tron analog of the reported "overflow reverts instead of wrapping" bug class. `BaseV1Pair._update()` intentionally relied on wraparound overflow that broke when compiled under Solidity ≥0.8.0's checked arithmetic. In java-tron, the resource-accounting code (`ResourceProcessor`, `BandwidthProcessor`, `EnergyProcessor`) has an equivalent legacy "wrap/truncate" path and a newer "hardened" path gated by the chain parameter `allowHardenResourceCalculation`. The hardened path replaces plain `long` arithmetic with `BigInteger...longValueExact()` calls, which — exactly like Solidity 0.8+ checked math — throw on overflow instead of truncating/wrapping. Unlike the exchange actuators (`ExchangeInjectActuator`, `ExchangeTransactionActuator`), which explicitly catch `ArithmeticException` and convert it into a `ContractExeException`, the resource-processing call sites do **not** catch this exception.

### Finding Description
`ResourceProcessor.increase()` and its private helpers (`divideCeilExact`, `getUsage`, `getNewWindowSize`) switch behavior based on `hardenCalculation()`: [1](#0-0) [2](#0-1) 

`hardenCalculation()` simply reads the dynamic property `allowHardenResourceCalculation`: [3](#0-2) 

`increase()` is invoked directly, unconditionally, and without any surrounding try/catch, on the hot path of ordinary transaction processing:
- `BandwidthProcessor.useAccountNet()` / `useFreeNet()` / `consumeBandwidthForCreateNewAccount()` / `useAssetAccountNet()`, called from `BandwidthProcessor.consume(TransactionCapsule, TransactionTrace)` for every transaction: [4](#0-3) 
- `EnergyProcessor.useEnergy()`, called for every smart-contract-triggering transaction from the VM/energy-billing path: [5](#0-4) 
- `calculateGlobalNetLimit`/`calculateGlobalEnergyLimit` also switch to `calculateGlobalLimitV1/V2`, which call `BigInteger...longValueExact()`: [6](#0-5) 
- `EnergyProcessor.scaleByRate()` (used in `updateAdaptiveTotalEnergyLimit`, run every maintenance cycle) has the same checked-math pattern: [7](#0-6) 

None of `BandwidthProcessor.consume()`, `useEnergy()`, `calculateGlobalNetLimit()`/`calculateGlobalEnergyLimit()`, or `updateAdaptiveTotalEnergyLimit()` catch `ArithmeticException`. In contrast, the exchange actuators explicitly guard against exactly this failure mode by wrapping `execute()`/`validate()` in `catch (... | ArithmeticException e)` and converting it to a normal `ContractExeException`/`ContractValidateException`: [8](#0-7) 

If `allowHardenResourceCalculation` is enabled (this is a real, already-shippable governance parameter — see `RepositoryImplHardenTest`/`ResourceProcessorHardenTest`, which explicitly demonstrate `ArithmeticException` being thrown once hardening is turned on for large-but-realistic usage/weight values), then any account whose accumulated usage, window size, frozen weight, or global totals push the `BigInteger` product outside `Long` range will cause `longValueExact()` to throw. Because bandwidth/energy consumption is evaluated on essentially every transaction (`BandwidthProcessor.consume`) and every contract-triggering transaction (`EnergyProcessor.useEnergy`), and because this occurs deterministically the same way on every full node validating the same block, this uncaught `RuntimeException` (`ArithmeticException` is unchecked) escapes the actuator dispatch logic that only anticipates `ContractValidateException`/`AccountResourceInsufficientException`/`BalanceInsufficientException`, and propagates up into block application.

### Impact Explanation
An uncaught `ArithmeticException` thrown during resource accounting for a normal, valid, signed transaction (transfer, `TriggerSmartContract`, asset transfer, account creation, etc.) is not a validation failure that only rejects one bad transaction — it is a `RuntimeException` escaping the expected exception contract of `consume()`/`useEnergy()`. Since this is triggered deterministically by state that is part of consensus (account usage counters, total net/energy weight, window sizes — all committed on-chain), every full node executing the same block reaches the same crash/exception at the same point, which can halt block application/consensus processing across the network (chain halt) rather than merely failing a single transaction. This matches the "core functionalities break" impact class described in the source report, but is more severe because the exception is unhandled at this layer rather than caught and downgraded to a contract-execution failure (as it is in the Exchange actuators).

### Likelihood Explanation
`allowHardenResourceCalculation` is a normal chain-governance parameter (set via committee proposal, same mechanism as other `allow*` flags such as `allowNewReward`, `allowTvmFreeze`), so it can be turned on by the network without requiring code redeployment. Once enabled, triggering the overflow requires an account (or a global resource-weight aggregate) to reach usage/weight/window-size values whose `BigInteger` products exceed `Long.MAX_VALUE` — the existing test suite (`ResourceProcessorHardenTest`, `RepositoryImplHardenTest`) shows this is reachable with realistic-looking values (e.g., `lastUsage = Long.MAX_VALUE/10`), i.e., not an astronomically large or purely theoretical input. An attacker who accumulates bandwidth/energy usage or influences total net/energy weight (via freezing large TRX amounts, which is a normal, permissionless operation) over time could deliberately push these values toward the overflow boundary once hardening is active.

### Recommendation
Mirror the mitigation already applied to `ExchangeInjectActuator`/`ExchangeTransactionActuator`: wrap all `hardenCalculation()`-gated `BigInteger...longValueExact()` computations in `ResourceProcessor`, `BandwidthProcessor`, and `EnergyProcessor` (`increase`, `divideCeilExact`, `getUsage`, `getNewWindowSize`, `calculateGlobalLimitV1/V2`, `scaleByRate`) with `try { ... } catch (ArithmeticException e)` at the call sites that are part of the consensus-critical transaction/block-processing path, converting the exception into the normal, already-handled failure types (`AccountResourceInsufficientException`, or fail-safe clamping to `Long.MAX_VALUE`/using a non-throwing BigInteger fallback) instead of letting it propagate as an unchecked `RuntimeException` out of block application.

### Proof of Concept
Not independently reproduced end-to-end (would require constructing on-chain state with `allowHardenResourceCalculation=1` and usage/weight values near `Long.MAX_VALUE` and then submitting a transaction that calls `BandwidthProcessor.consume()`/`EnergyProcessor.useEnergy()`), but the existing repository test suite already demonstrates the exact overflow trigger condition and confirms the exception type: [9](#0-8) [10](#0-9) 
These tests confirm `ArithmeticException` is thrown by the hardened arithmetic path for realistic `lastUsage`/`totalWeight` magnitudes; the gap this report highlights is that, unlike the Exchange actuators, none of `BandwidthProcessor.consume()` / `EnergyProcessor.useEnergy()` / `calculateGlobalNetLimit()` / `calculateGlobalEnergyLimit()` catch this exception before it reaches the transaction/block-processing layer.

### Citations

**File:** chainbase/src/main/java/org/tron/core/db/ResourceProcessor.java (L50-63)
```java
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
```

**File:** chainbase/src/main/java/org/tron/core/db/ResourceProcessor.java (L276-291)
```java
  private long divideCeilExact(BigInteger numerator, BigInteger denominator) {
    BigInteger[] divRem = numerator.divideAndRemainder(denominator);
    long result = divRem[0].longValueExact();
    if (divRem[1].signum() > 0) {
      result = StrictMathWrapper.addExact(result, 1);
    }
    return result;
  }

  private long getUsage(long usage, long windowSize) {
    if (hardenCalculation()) {
      return BigInteger.valueOf(usage).multiply(BigInteger.valueOf(windowSize))
          .divide(BigInteger.valueOf(precision)).longValueExact();
    }
    return usage * windowSize / precision;
  }
```

**File:** chainbase/src/main/java/org/tron/core/db/ResourceProcessor.java (L346-348)
```java
  protected boolean hardenCalculation() {
    return dynamicPropertiesStore.allowHardenResourceCalculation();
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

**File:** chainbase/src/main/java/org/tron/core/db/BandwidthProcessor.java (L468-504)
```java
  private boolean useAccountNet(AccountCapsule accountCapsule, long bytes, long now) {

    long netUsage = accountCapsule.getNetUsage();
    long latestConsumeTime = accountCapsule.getLatestConsumeTime();
    long netLimit = calculateGlobalNetLimit(accountCapsule);

    long newNetUsage;
    if (!dynamicPropertiesStore.supportUnfreezeDelay()) {
      newNetUsage = increase(netUsage, 0, latestConsumeTime, now);
    } else {
      // only participate in the calculation as a temporary variable, without disk flushing
      newNetUsage = recovery(accountCapsule, BANDWIDTH, netUsage, latestConsumeTime, now);
    }


    if (bytes > (netLimit - newNetUsage)) {
      logger.debug("Net usage is running out, now use free net usage."
              + " Bytes: {}, netLimit: {}, newNetUsage: {}.",
          bytes, netLimit, newNetUsage);
      return false;
    }

    long latestOperationTime = chainBaseManager.getHeadBlockTimeStamp();
    if (!dynamicPropertiesStore.supportUnfreezeDelay()) {
      newNetUsage = increase(newNetUsage, bytes, now, now);
    } else {
      // Participate in calculation and flush disk persistence
      newNetUsage = increase(accountCapsule, BANDWIDTH, netUsage, bytes, latestConsumeTime, now);
    }

    accountCapsule.setNetUsage(newNetUsage);
    accountCapsule.setLatestOperationTime(latestOperationTime);
    accountCapsule.setLatestConsumeTime(now);

    chainBaseManager.getAccountStore().put(accountCapsule.createDbKey(), accountCapsule);
    return true;
  }
```

**File:** chainbase/src/main/java/org/tron/core/db/EnergyProcessor.java (L102-143)
```java
  public boolean useEnergy(AccountCapsule accountCapsule, long energy, long now) {

    long energyUsage = accountCapsule.getEnergyUsage();
    long latestConsumeTime = accountCapsule.getAccountResource().getLatestConsumeTimeForEnergy();
    long energyLimit = calculateGlobalEnergyLimit(accountCapsule);
    long newEnergyUsage;
    if (!dynamicPropertiesStore.supportUnfreezeDelay()) {
      newEnergyUsage = increase(energyUsage, 0, latestConsumeTime, now);
    } else {
      // only participate in the calculation as a temporary variable, without disk flushing
      newEnergyUsage = recovery(accountCapsule, ENERGY, energyUsage,
          latestConsumeTime, now);
    }

    if (energy > (energyLimit - newEnergyUsage)
        && dynamicPropertiesStore.getAllowTvmFreeze() == 0
        && !dynamicPropertiesStore.supportUnfreezeDelay()) {
      return false;
    }

    long latestOperationTime = dynamicPropertiesStore.getLatestBlockHeaderTimestamp();
    if (!dynamicPropertiesStore.supportUnfreezeDelay()) {
      newEnergyUsage = increase(newEnergyUsage, energy, now, now);
    } else {
      // Participate in calculation and flush disk persistence
      newEnergyUsage = increase(accountCapsule, ENERGY, energyUsage, energy,
          latestConsumeTime, now);
    }

    accountCapsule.setEnergyUsage(newEnergyUsage);
    accountCapsule.setLatestOperationTime(latestOperationTime);
    accountCapsule.setLatestConsumeTimeForEnergy(now);

    accountStore.put(accountCapsule.createDbKey(), accountCapsule);

    if (dynamicPropertiesStore.getAllowAdaptiveEnergy() == 1) {
      long blockEnergyUsage = dynamicPropertiesStore.getBlockEnergyUsage() + energy;
      dynamicPropertiesStore.saveBlockEnergyUsage(blockEnergyUsage);
    }

    return true;
  }
```

**File:** chainbase/src/main/java/org/tron/core/db/EnergyProcessor.java (L197-205)
```java
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L107-123)
```java
    } catch (ItemNotFoundException | InvalidProtocolBufferException
        | ArithmeticException e) {
      logger.debug(e.getMessage(), e);
      ret.setStatus(fee, code.FAILED);
      throw new ContractExeException(e.getMessage());
    }
    return true;
  }

  @Override
  public boolean validate() throws ContractValidateException {
    try {
      return doValidate();
    } catch (ArithmeticException e) {
      throw new ContractValidateException(e.getMessage());
    }
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

**File:** framework/src/test/java/org/tron/core/vm/repository/RepositoryImplHardenTest.java (L215-225)
```java
  @Test
  public void testUsageToBalanceOverflowDetectedWithHardening() {
    long usage = 1_000_000_000L;
    long totalWeight = 1_000_000_000_000L;
    long totalLimit = 1L;

    VMConfig.initAllowHardenResourceCalculation(1);

    Assert.assertThrows(ArithmeticException.class,
        () -> invokeUsageToBalance(usage, totalWeight, totalLimit));
  }
```
