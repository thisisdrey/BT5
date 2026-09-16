### Title
Hardened resource-usage accounting can throw uncaught `ArithmeticException`, blocking bandwidth/energy consumption and withdrawal operations for the affected account - (File: `chainbase/src/main/java/org/tron/core/db/ResourceProcessor.java`)

### Summary
The Euler finding describes `initVaultCache`, a routine invoked before every vault operation, that can revert on arithmetic overflow, thereby breaking the liveness guarantee that `withdraw`/`redeem`/`liquidate` must always be executable. java-tron has an analogous pattern: the "hardened" bandwidth/energy usage-accounting routines in `ResourceProcessor` (and its mirror in `RepositoryImpl` for the TVM path) recompute an account's resource usage on *every* transaction using `BigInteger` arithmetic terminated by `.longValueExact()`. Unlike the legacy `long` arithmetic (which silently wraps on overflow), the hardened path deliberately throws `ArithmeticException` when the computed value does not fit into a `long`. This exception is not caught at any of the call sites that invoke resource accounting ahead of transaction/actuator execution, so an account whose usage/window-size bookkeeping reaches the overflow threshold can be permanently unable to submit *any* transaction — including transactions intended to fix its state, such as `WithdrawExpireUnfreezeContract` / `UnfreezeBalanceV2Contract` / TVM `unfreezeBalanceV2`/`withdrawExpireUnfreeze` — mirroring the "overflow reverts operations that must always be possible" liveness violation described in the report.

### Finding Description
`ResourceProcessor.increase()` / `increaseV2()` compute a new resource-usage value for an account before every bandwidth/energy consumption: [1](#0-0) 

When `hardenCalculation()` (backed by `allowHardenResourceCalculation`) is enabled, the intermediate products are computed with `BigInteger` and then converted back to `long` via `divideCeilExact`, which explicitly throws `ArithmeticException` if the true quotient does not fit into a `long`: [2](#0-1) 

The same pattern recurs in `increaseV2`, `getNewWindowSize`, `getUsage`, and the TVM-side mirror `RepositoryImpl.increase()`/`getUsage()`/`calculateGlobalEnergyLimit()`, all of which are exercised on essentially every transaction that consumes or recovers bandwidth/energy: [3](#0-2) [4](#0-3) 

Project unit tests confirm the hardened path is designed to throw on overflow where the legacy path silently wraps: [5](#0-4) [6](#0-5) 

Critically, none of the call sites that invoke `BandwidthProcessor`/`EnergyProcessor` `consume()`/`updateUsage()` in `Manager.java`, nor the TVM callers of `RepositoryImpl.increase()`/`getAccountLeftEnergyFromFreeze()`, wrap these calls in a `try/catch(ArithmeticException)` — the grep for `catch (ArithmeticException` across the codebase shows such handling only around narrow, explicit `LongMath.checkedAdd`/balance calls inside individual actuators (e.g. `WithdrawBalanceActuator`, `WithdrawExpireUnfreezeActuator`, `WithdrawRewardProcessor`), not around the resource-usage recomputation performed before those actuators run. If an account's `newWindowSize`/usage bookkeeping (built up through legitimate freeze/delegate/undelegate/cancel-unfreeze operations that themselves use unguarded `long` multiplication such as `ownerUsage * remainOwnerWindowSizeV2` in `increaseV2`) reaches a magnitude where the hardened recomputation overflows a `long`, every subsequent transaction from that account that triggers bandwidth/energy accounting — including the very transactions meant to unfreeze/withdraw funds (`WithdrawExpireUnfreezeContract`, `UnfreezeBalanceV2Contract`, TVM `unfreezeBalanceV2`) — will throw an uncaught `ArithmeticException` instead of completing, in direct analogy to the Euler `initVaultCache` liveness break.

### Impact Explanation
This is a liveness/availability issue: an account whose resource-accounting state overflows the hardened arithmetic can no longer execute basic operations that should always remain available, including recovering frozen TRX via `WithdrawExpireUnfreezeContract`/`UnfreezeBalanceV2Contract`. Depending on where the uncaught exception ultimately propagates (transaction application vs. block application), this could also manifest as permanent freezing of funds for the affected account or unexpected transaction failures that are inconsistent with the legacy (non-hardened) code path's silently-wrapped behavior, breaking parity between the two configured modes.

### Likelihood Explanation
Reaching the overflow threshold requires the account's usage/window-size state to grow to a magnitude near `Long.MAX_VALUE` through repeated freeze/unfreeze/delegate/undelegate cycles, which is a comparatively high bar under normal economic constraints of TRX supply, but the enabling condition (`allowHardenResourceCalculation`) is a chain-parameter toggle, and the arithmetic paths that build up these usage/window values themselves use unguarded `long` multiplication in non-hardened branches (e.g. `ownerUsage * remainOwnerWindowSizeV2 + transferUsage * remainReceiverWindowSizeV2` in `increaseV2`), meaning corrupted or extreme intermediate state is plausible over long-running accounts with large frozen balances and repeated resource delegation. I was not able to fully verify, within the available tool budget, whether any outer exception handler in `Manager.java`'s block-application path ultimately catches a generic `RuntimeException`/`ArithmeticException` and merely fails the single transaction versus halting block processing — this distinguishes a "medium" (transaction/account liveness) impact from a more severe "chain halt" impact, and should be confirmed by tracing `Manager.processTransaction` / `pushTransaction` exception handling.

### Recommendation
1. Audit all call sites of `ResourceProcessor.increase()/increaseV2()`, `RepositoryImpl.increase()/getUsage()/calculateGlobalEnergyLimit()` in `Manager.java` and the TVM `Program`/`Repository` layer, and ensure `ArithmeticException` from the hardened path cannot abort essential recovery operations (`WithdrawExpireUnfreezeContract`, `UnfreezeBalanceV2Contract`, TVM `unfreezeBalanceV2`/`withdrawExpireUnfreeze`).
2. For these specific liveness-critical paths, prefer a saturating/clamped computation (as the legacy path implicitly does) over a hard revert, or explicitly catch `ArithmeticException` and fall back to a safe clamped value so that withdrawal/unfreeze functionality remains available even if usage bookkeeping has reached extreme values.
3. Add invariant checks/tests that simulate long-running accounts approaching the overflow boundary and assert that `WithdrawExpireUnfreeze`/`UnfreezeBalanceV2` still succeed under `allowHardenResourceCalculation`.
4. Document, similar to the Euler whitepaper update, the liveness guarantees expected under the hardened-vs-legacy resource-calculation flags, and reconcile the two paths' error-handling behavior (silent wrap vs. throw) — currently `ResourceProcessorHardenTest` explicitly documents this divergence as expected (`testIncreaseOverflowSilentWithoutHardening` vs. `testIncreaseOverflowDetectedWithHardening`), which is the same tension flagged by the Spearbit report.

### Proof of Concept
Conceptual PoC (not fully executable within this investigation's scope):
1. Enable `allowHardenResourceCalculation` (chain parameter toggled by committee, or already default in the target network).
2. Over many blocks, an account repeatedly freezes/unfreezes/delegates and undelegates large TRX amounts for `ENERGY`/`BANDWIDTH`, driving `ownerUsage`, `remainOwnerWindowSizeV2`, or similar internal usage/window fields toward magnitudes that make the `BigInteger` product in `increaseV2`/`getNewWindowSize` exceed `Long.MAX_VALUE` when divided back (see `ResourceProcessor.java:262-270`, `133-188`).
3. Any subsequent transaction from this account that triggers bandwidth/energy consumption (a normal transfer, or specifically a `WithdrawExpireUnfreezeContract`/`UnfreezeBalanceV2Contract`) causes `divideCeilExact`/`longValueExact()` to throw `ArithmeticException`.
4. Because none of the call sites feeding into `BandwidthProcessor`/`EnergyProcessor`/`RepositoryImpl` catch this exception around the usage-recomputation call (only around isolated balance-add checks in the actuators themselves), the transaction fails — including the withdrawal/unfreeze transaction meant to reduce the account's exposure, reproducing the "operation that should always be possible now reverts" liveness break described in the source report. Confirming the full end-to-end reachability (including exact overflow thresholds and Manager-level exception propagation) would require executing the referenced `ResourceProcessorHardenTest`/`RepositoryImplHardenTest` overflow tests against a live transaction-processing harness, which was not performed here.

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

**File:** chainbase/src/main/java/org/tron/core/db/ResourceProcessor.java (L133-188)
```java
  public long increaseV2(AccountCapsule accountCapsule, ResourceCode resourceCode,
      long lastUsage, long usage, long lastTime, long now) {
    long oldWindowSizeV2 = accountCapsule.getWindowSizeV2(resourceCode);
    long oldWindowSize = accountCapsule.getWindowSize(resourceCode);
    long averageLastUsage;
    long averageUsage;
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

    if (lastTime != now) {
      if (lastTime + oldWindowSize > now) {
        long delta = now - lastTime;
        double decay = (oldWindowSize - delta) / (double) oldWindowSize;
        averageLastUsage = round(averageLastUsage * decay,
            this.disableJavaLangMath());
      } else {
        averageLastUsage = 0;
      }
    }

    long newUsage = getUsage(averageLastUsage, oldWindowSize, averageUsage, this.windowSize);
    long remainUsage = getUsage(averageLastUsage, oldWindowSize);
    if (remainUsage == 0) {
      accountCapsule.setNewWindowSizeV2(resourceCode, this.windowSize * WINDOW_SIZE_PRECISION);
      return newUsage;
    }

    long remainWindowSize = oldWindowSizeV2 - (now - lastTime) * WINDOW_SIZE_PRECISION;
    long newWindowSize;
    if (hardenCalculation()) {
      BigInteger biNewWindowSize = BigInteger.valueOf(remainUsage)
          .multiply(BigInteger.valueOf(remainWindowSize))
          .add(BigInteger.valueOf(usage)
              .multiply(BigInteger.valueOf(this.windowSize))
              .multiply(BigInteger.valueOf(WINDOW_SIZE_PRECISION)));
      newWindowSize = divideCeilExact(biNewWindowSize, BigInteger.valueOf(newUsage));
    } else {
      newWindowSize = divideCeil(
          remainUsage * remainWindowSize + usage * this.windowSize * WINDOW_SIZE_PRECISION,
          newUsage);
    }
    newWindowSize = min(newWindowSize, this.windowSize * WINDOW_SIZE_PRECISION,
        this.disableJavaLangMath());
    accountCapsule.setNewWindowSizeV2(resourceCode, newWindowSize);
    return newUsage;
  }
```

**File:** chainbase/src/main/java/org/tron/core/db/ResourceProcessor.java (L276-283)
```java
  private long divideCeilExact(BigInteger numerator, BigInteger denominator) {
    BigInteger[] divRem = numerator.divideAndRemainder(denominator);
    long result = divRem[0].longValueExact();
    if (divRem[1].signum() > 0) {
      result = StrictMathWrapper.addExact(result, 1);
    }
    return result;
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/repository/RepositoryImpl.java (L932-963)
```java
  private long recover(long lastUsage, long lastTime, long now, long personalWindowSize) {
    return increase(lastUsage, 0, lastTime, now, personalWindowSize);
  }

  private long increase(long lastUsage, long usage, long lastTime, long now, long windowSize) {
    long averageLastUsage;
    long averageUsage;
    if (hardenResourceCalculation()) {
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
        averageLastUsage = round(averageLastUsage * decay, VMConfig.disableJavaLangMath());
      } else {
        averageLastUsage = 0;
      }
    }
    averageLastUsage += averageUsage;
    return getUsage(averageLastUsage, windowSize);
  }
```

**File:** framework/src/test/java/org/tron/core/db/ResourceProcessorHardenTest.java (L106-130)
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

  @Test
  public void testIncreaseOverflowSilentWithoutHardening() {
    long lastUsage = Long.MAX_VALUE / 10;
    long usage = 1L;
    long lastTime = 9990L;
    long now = 9995L;
    long windowSize = 28800L;

    dbManager.getDynamicPropertiesStore().saveAllowHardenResourceCalculation(0);
    processor.increase(lastUsage, usage, lastTime, now, windowSize);
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
