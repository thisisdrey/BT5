### Title
Resource-usage window recalculation trusts `now >= lastTime` without enforcing it, allowing corrupted bandwidth/energy accounting - ([File: chainbase/src/main/java/org/tron/core/db/ResourceProcessor.java])

### Summary
`ResourceProcessor.increase(long,long,long,long,long)` contains only an `assert now > lastTime;` guard for the ordering assumption between the account's stored `latestConsumeTime`/`latestConsumeTimeForEnergy` ("lastTime") and the current head-slot value ("now") [1](#0-0) . Java `assert` statements are compiled out and disabled at runtime unless the JVM is started with `-ea`, which is not the default for production nodes, so this check is effectively a no-op. Worse, the disk-persisted variants used by the actual bandwidth/energy consumption paths, `increase(AccountCapsule, ResourceCode, long, long, long, long)` and `increaseV2(...)`, don't even have the assert — they unconditionally compute a decay factor from `now - lastTime` and derive a new `windowSize`/usage value with no check that `now` is not older than the previously recorded `lastTime` [2](#0-1) [3](#0-2) . This mirrors the CVE-2022-49935 bug class: code assumes a monotonically increasing "fence"/counter (here, a per-account consume timestamp) is always later than the previously stored one, and omits a real runtime check for that invariant.

### Finding Description
`BandwidthProcessor.useAccountNet`, `BandwidthProcessor.useFreeNet`, `EnergyProcessor.useEnergy`, and the TVM-side `DelegateResourceProcessor`/`UnDelegateResourceProcessor` all call into `ResourceProcessor.increase`/`increaseV2` with `now` derived from the current head slot and `lastTime` read from the account's persisted `latestConsumeTime`/`latestConsumeTimeForEnergy` fields [4](#0-3) [5](#0-4) .

The decay math is:
```
if (lastTime != now) {
  if (lastTime + oldWindowSize > now) {
    long delta = now - lastTime;
    double decay = (oldWindowSize - delta) / (double) oldWindowSize;
    averageLastUsage = round(averageLastUsage * decay, ...);
  } else {
    averageLastUsage = 0;
  }
}
```
This logic only produces a sane bounded result (0 ≤ decay ≤ 1) when `now >= lastTime`. If `now < lastTime` — i.e., the "fence" ordering assumption is violated — `delta` becomes negative and `decay` becomes greater than 1, inflating `averageLastUsage` in the accounting path [6](#0-5) . The magnitude of the inflation grows unboundedly the larger `lastTime - now` is, and this feeds directly into `getNewWindowSize`/`newWindowSize` computations that are persisted back onto the account's `windowSize`/`windowSizeV2` fields via `setNewWindowSize`/`setNewWindowSizeV2` [7](#0-6) , and into `newEnergyUsage`/`newNetUsage` that are compared against `energyLimit`/`netLimit` to decide whether a transaction is allowed to consume bandwidth/energy for free [8](#0-7) [9](#0-8) .

Because the only enforcement of the "now is always later" invariant is an `assert` (disabled in production JVMs by default) rather than a real conditional/exception, any code path that can cause a decreasing sequence of `now` values relative to a stored `lastTime` for the same account (e.g. divergent timestamp/slot sources feeding `now` across `BandwidthProcessor`, `EnergyProcessor`, `ResourceProcessor` and the TVM `RepositoryImpl`/native-contract resource processors, or reprocessing of a transaction/account state under an inconsistent head slot) will silently corrupt persisted usage/windowSize accounting instead of failing safe.

### Impact Explanation
If `newEnergyUsage`/`newNetUsage` becomes corrupted (via inflated `averageLastUsage` feeding into `getUsage`) it can end up negative or wildly out of range due to overflow/rounding, and negative usage values pass the `energy > (energyLimit - newEnergyUsage)` / `bytes > (netLimit - newNetUsage)` checks trivially, letting an account bypass bandwidth/energy metering (free unlimited TVM execution or bandwidth consumption) [8](#0-7) . Persisted corrupted `windowSize`/`windowSizeV2` values are also later used as divisors in subsequent `increase`/`increaseV2` calls (`divideCeil(... , oldWindowSize)`), so a corrupted `windowSize` of `0` would throw an `ArithmeticException` on the next resource-consuming transaction for that account, effectively freezing that account's future transactions/energy usage — a persistent denial-of-service condition reachable purely from unprivileged transaction broadcasting (freeze/delegate/unfreeze/transfer/contract-call flows).

### Likelihood Explanation
I was not able to fully verify, within the available investigation, a concrete existing code path in this repository version that produces `now < lastTime` for the same account in practice — `now` is generally derived consistently from `chainBaseManager.getHeadSlot()`/`dynamicPropertiesStore.getLatestBlockHeaderTimestamp()`, which is monotonic with the chain head under normal single-path operation. The exploitability therefore hinges on finding a concrete divergence between the several parallel "now" computations (`ChainBaseManager.getHeadSlot()`, `EnergyProcessor.getHeadSlot(store)`, `RepositoryImpl.getHeadSlot()`, and ms-based `getLatestBlockHeaderTimestamp()` used elsewhere for expiry math) that could feed a lower `now` into `increase`/`increaseV2` than a previously stored `lastTime` for the same account/resource. I could not confirm such a divergence exists in reachable production code within this analysis; the report should be treated as identifying a defense-in-depth gap (assert-only enforcement of a critical invariant on a disk-persisted accounting path) rather than a fully proven exploit chain.

### Recommendation
- Replace the `assert now > lastTime;` (and the missing equivalent guard in `increase`/`increaseV2`, `recovery`, and `useAssetAccountNet`'s issuer-usage recomputation) with an explicit runtime check (`if (now < lastTime) { ... }`) that either clamps/rejects the operation or throws a checked exception, mirroring the dma-buf fix of validating that the new fence/timestamp is actually later before trusting it.
- Add invariant checks after computing `windowSize`/`windowSizeV2`/`newNetUsage`/`newEnergyUsage` to reject or clamp negative/degenerate results before persisting them to `AccountCapsule`.
- Audit all call sites of `getHeadSlot()`/`getLatestBlockHeaderTimestamp()` feeding these accounting functions to confirm a single, monotonic time source is used consistently across `BandwidthProcessor`, `EnergyProcessor`, `ResourceProcessor`, and `RepositoryImpl`.

### Proof of Concept
Not able to construct a concrete end-to-end transaction sequence within this analysis that forces `now < lastTime` for the same account through the currently reachable actuator/native-contract paths; the finding is based on static code review of the missing/ineffective ordering check in `ResourceProcessor.increase`/`increaseV2` [10](#0-9) . A background engineering session with tooling access (to run the existing `BandWidthRuntimeTest`/`EnergyProcessorTest`/`DelegateResourceActuatorTest` suites and simulate divergent `now` values across `ChainBaseManager.getHeadSlot()` vs `RepositoryImpl.getHeadSlot()` calls in the same transaction) would be needed to confirm reachability and construct a concrete PoC transaction.

### Citations

**File:** chainbase/src/main/java/org/tron/core/db/ResourceProcessor.java (L65-131)
```java
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

  public long recovery(AccountCapsule accountCapsule, ResourceCode resourceCode,
      long lastUsage, long lastTime, long now) {
    long oldWindowSize = accountCapsule.getWindowSize(resourceCode);
    return increase(lastUsage, 0, lastTime, now, oldWindowSize);
  }

  public long increase(AccountCapsule accountCapsule, ResourceCode resourceCode,
      long lastUsage, long usage, long lastTime, long now) {
    if (dynamicPropertiesStore.supportAllowCancelAllUnfreezeV2()) {
      return increaseV2(accountCapsule, resourceCode, lastUsage, usage, lastTime, now);
    }
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
    if (dynamicPropertiesStore.supportUnfreezeDelay()) {
      long remainUsage = getUsage(averageLastUsage, oldWindowSize);
      if (remainUsage == 0) {
        accountCapsule.setNewWindowSize(resourceCode, this.windowSize);
        return newUsage;
      }
      long remainWindowSize = oldWindowSize - (now - lastTime);
      long newWindowSize = getNewWindowSize(remainUsage, remainWindowSize, usage,
          windowSize, newUsage);
      accountCapsule.setNewWindowSize(resourceCode, newWindowSize);
    }
    return newUsage;
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

**File:** chainbase/src/main/java/org/tron/core/db/BandwidthProcessor.java (L468-488)
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
```

**File:** chainbase/src/main/java/org/tron/core/db/EnergyProcessor.java (L102-120)
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
```
