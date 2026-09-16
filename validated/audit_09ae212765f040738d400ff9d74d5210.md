### Title
Hardened adaptive-energy-limit computation can throw an uncaught ArithmeticException in `EnergyProcessor.updateAdaptiveTotalEnergyLimit`, crashing the node during block processing - (File: chainbase/src/main/java/org/tron/core/db/EnergyProcessor.java)

### Summary
`EnergyProcessor.updateAdaptiveTotalEnergyLimit()` recomputes the adaptive total-energy limit every maintenance cycle, analogous to how the reported CrabStrategy/Controller bug recomputes the normalization factor on every relevant operation. When `ALLOW_HARDEN_RESOURCE_CALCULATION` is enabled, the multiplier/upper-bound computation switches from raw `long` arithmetic to `BigInteger.longValueExact()`/`StrictMathWrapper`-based "hardened" math that throws `ArithmeticException` on overflow instead of silently wrapping. Just like the report's corner case (`mark == lowerBound && period >= 346279` breaking the invariant the code silently assumed), the hardened path here assumes the multiplied `totalEnergyLimit * multiplier` value always fits in a `long`; when governance-controlled parameters push the product past `Long.MAX_VALUE`, `longValueExact()` throws, and this exception is not caught anywhere in the call chain.

### Finding Description
`updateAdaptiveTotalEnergyLimit()` computes: [1](#0-0) 

The `upperBound` is computed via `BigInteger.valueOf(totalEnergyLimit).multiply(BigInteger.valueOf(adaptiveResourceLimitMultiplier)).longValueExact()` when hardening is enabled [2](#0-1) . Both `totalEnergyLimit` (via `TOTAL_ENERGY_LIMIT`/`TOTAL_CURRENT_ENERGY_LIMIT` proposals) and `ADAPTIVE_RESOURCE_LIMIT_MULTIPLIER` (range `[1, 10000]`) are governance-settable chain parameters reachable through committee proposals processed in `ProposalService.process` [3](#0-2) . The multiplier proposal is validated only for a bounded numeric range in `ProposalUtil`, not for compatibility with the current `totalEnergyLimit`, so a combination that individually passes validation can still overflow `long` when multiplied together (e.g., `totalEnergyLimit` near `10^16` and multiplier `10000`, as directly reproduced in the existing test).

This mirrors the audited bug pattern precisely: the code silently assumed an arithmetic relationship (`totalEnergyLimit * multiplier` fits in `long`) holds under all "legal" governance-configured values, and only added a strict/hardened check that throws instead of preventing the unsafe state from being reachable in the first place. The existing regression test in this exact repository already demonstrates the throw: [4](#0-3) 

`updateAdaptiveTotalEnergyLimit()` is invoked from `Manager.java` during normal block/maintenance processing (confirmed by a match in `framework/src/main/java/org/tron/core/db/Manager.java`), meaning the throw occurs inside the block-application path rather than inside a single user transaction's try/catch. Unlike the exchange actuators (`ExchangeTransactionActuator`, `ExchangeWithdrawActuator`, `ExchangeInjectActuator`) which explicitly catch `ArithmeticException` and convert it into a graceful `ContractExeException`/`ContractValidateException` per-transaction failure [5](#0-4) , no such catch exists around the maintenance-cycle adaptive-limit recomputation, so an uncaught `ArithmeticException` there would propagate up through block processing.

### Impact Explanation
If `updateAdaptiveTotalEnergyLimit()` throws during maintenance-cycle processing without being caught, every witness node executing that maintenance step would fail identically (since it's deterministic on-chain state), which would either halt block production/validation network-wide or cause consensus-breaking divergence if only some nodes have hardening enabled via the feature-gated `ALLOW_HARDEN_RESOURCE_CALCULATION` proposal. This matches the "node crash or halt" / "chain split" impact bar, analogous to the Controller contract becoming permanently blocked in the original report.

### Likelihood Explanation
Reaching this requires: (1) the `ALLOW_HARDEN_RESOURCE_CALCULATION` chain parameter to be enabled via committee proposal, and (2) `TOTAL_ENERGY_LIMIT`/`TOTAL_CURRENT_ENERGY_LIMIT` and `ADAPTIVE_RESOURCE_LIMIT_MULTIPLIER` to be set (each individually within their allowed proposal ranges) such that their product overflows `long`. Both parameters are governance/committee-controlled rather than directly settable by an arbitrary unprivileged transaction, so the likelihood depends on committee proposal actions rather than a single unprivileged broadcast — this weakens direct reachability by an anonymous API client compared to the original report's scenario (which was purely time/price driven with no governance gate). I could not fully verify within the available index whether `TOTAL_ENERGY_LIMIT`'s proposal validation bounds (`[0, 100000000000000000]` per the `ProposalType` enum comment) combined with multiplier `10000` can occur under currently-live production values, or whether `ALLOW_HARDEN_RESOURCE_CALCULATION` is already enabled on mainnet — this needs further confirmation.

### Recommendation
- Add an explicit bounds check on the `totalEnergyLimit * adaptiveResourceLimitMultiplier` product before/within the `ADAPTIVE_RESOURCE_LIMIT_MULTIPLIER`/`TOTAL_ENERGY_LIMIT` proposal validators in `ProposalUtil`, rejecting proposals that would cause an overflow given the current value of the other parameter.
- Wrap `updateAdaptiveTotalEnergyLimit()` (and other maintenance-cycle hardened-math calls) in a try/catch that logs and falls back to a safe/clamped value rather than propagating an uncaught `ArithmeticException` through block processing.
- Add Echidna/fuzz-style invariant tests combining `TOTAL_ENERGY_LIMIT`, `TOTAL_CURRENT_ENERGY_LIMIT`, and `ADAPTIVE_RESOURCE_LIMIT_MULTIPLIER` proposal value ranges to confirm no combination of individually-valid values can overflow the hardened multiplication.

### Proof of Concept
The existing test in this repository already demonstrates the crash condition directly: [6](#0-5) 

Setting `totalEnergyLimit = 10_000_000_000_000_000L` and `adaptiveResourceLimitMultiplier = 1000L` with hardening enabled causes `EnergyProcessor.updateAdaptiveTotalEnergyLimit()` to throw `ArithmeticException` via `longValueExact()` [2](#0-1) . Since these values are individually within governance-proposable ranges and this method runs unconditionally in the block/maintenance-processing path (not inside a per-transaction try/catch), triggering this state via committee proposals would cause the exception to propagate during normal chain operation instead of being isolated to a single failed transaction.

### Citations

**File:** chainbase/src/main/java/org/tron/core/db/EnergyProcessor.java (L65-93)
```java
  public void updateAdaptiveTotalEnergyLimit() {
    long totalEnergyAverageUsage = dynamicPropertiesStore
        .getTotalEnergyAverageUsage();
    long targetTotalEnergyLimit = dynamicPropertiesStore.getTotalEnergyTargetLimit();
    long totalEnergyCurrentLimit = dynamicPropertiesStore
        .getTotalEnergyCurrentLimit();
    long totalEnergyLimit = dynamicPropertiesStore.getTotalEnergyLimit();

    long result;
    if (totalEnergyAverageUsage > targetTotalEnergyLimit) {
      result = scaleByRate(totalEnergyCurrentLimit,
          AdaptiveResourceLimitConstants.CONTRACT_RATE_NUMERATOR,
          AdaptiveResourceLimitConstants.CONTRACT_RATE_DENOMINATOR);
    } else {
      result = scaleByRate(totalEnergyCurrentLimit,
          AdaptiveResourceLimitConstants.EXPAND_RATE_NUMERATOR,
          AdaptiveResourceLimitConstants.EXPAND_RATE_DENOMINATOR);
    }
    long upperBound = hardenCalculation()
        ? BigInteger.valueOf(totalEnergyLimit).multiply(BigInteger.valueOf(
            dynamicPropertiesStore.getAdaptiveResourceLimitMultiplier())).longValueExact()
        : totalEnergyLimit * dynamicPropertiesStore.getAdaptiveResourceLimitMultiplier();
    result = min(max(result, totalEnergyLimit, this.disableJavaLangMath()),
        upperBound, this.disableJavaLangMath());

    dynamicPropertiesStore.saveTotalEnergyCurrentLimit(result);
    logger.debug("Adjust totalEnergyCurrentLimit, old: {}, new: {}.",
        totalEnergyCurrentLimit, result);
  }
```

**File:** framework/src/main/java/org/tron/core/consensus/ProposalService.java (L111-122)
```java
        case TOTAL_ENERGY_LIMIT: {
          manager.getDynamicPropertiesStore().saveTotalEnergyLimit(entry.getValue());
          break;
        }
        case ALLOW_TVM_TRANSFER_TRC10: {
          manager.getDynamicPropertiesStore().saveAllowTvmTransferTrc10(entry.getValue());
          break;
        }
        case TOTAL_CURRENT_ENERGY_LIMIT: {
          manager.getDynamicPropertiesStore().saveTotalEnergyLimit2(entry.getValue());
          break;
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L100-105)
```java
    } catch (ItemNotFoundException | InvalidProtocolBufferException
        | ContractValidateException | ArithmeticException e) {
      logger.debug(e.getMessage(), e);
      ret.setStatus(fee, code.FAILED);
      throw new ContractExeException(e.getMessage());
    }
```
