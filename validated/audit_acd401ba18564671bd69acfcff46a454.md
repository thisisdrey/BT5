Based on my investigation, the `getNewWindowSize` division in `ResourceProcessor.unDelegateIncrease` is the strongest analog to the CVE's division-by-zero bug class, since `newOwnerUsage` (an attacker-influenced accumulator of resource usage) is used as a divisor without a zero-guard on that specific path.

### Title
Unguarded division by `newUsage`/`newOwnerUsage` in resource window-size recalculation can throw an uncaught ArithmeticException during transaction processing - (File: chainbase/src/main/java/org/tron/core/db/ResourceProcessor.java)

### Summary
`ResourceProcessor.getNewWindowSize()` and the sibling window-size math in `increaseV2()` divide by a usage value (`newUsage` / `newOwnerUsage`) derived from account-controlled resource counters (bandwidth/energy usage, transfer usage from delegate/undelegate) without verifying the divisor is non-zero before the non-hardened path executes `(lastUsage * lastWindowSize + usage * windowSize) / newUsage` [1](#0-0) , mirroring the CVE-2019-14249 pattern of trusting an untrusted/derived size field as a divisor without validating it is non-zero.

### Finding Description
`getNewWindowSize` computes `(lastUsage * lastWindowSize + usage * windowSize) / newUsage` [1](#0-0) . It is invoked from `increase()` only after `getUsage(...)` returns non-zero for `remainUsage` (guarded), but `unDelegateIncrease` calls it directly with `newOwnerUsage` as the divisor after only checking `newOwnerUsage == 0` earlier in the same method for early return [2](#0-1) . Likewise `increaseV2` divides by `newUsage` in the hardened and non-hardened branches with only a `remainUsage == 0` check, not a `newUsage == 0` check, before calling `divideCeil`/`divideCeilExact` [3](#0-2) . Similar unguarded divisions by `windowSize`/`oldWindowSize` exist in `increase()` and the duplicate implementation in `actuator/src/main/java/org/tron/core/vm/repository/RepositoryImpl.java` (`divideCeil`, `getUsage`) [4](#0-3) . These paths are reachable through ordinary `DelegateResourceContract`/`UnDelegateResourceContract` transactions and TVM's native `UnDelegateResourceProcessor` [5](#0-4) , both broadcastable by any unprivileged account.

### Impact Explanation
If any of these usage/window-size values can reach zero along a path that skips the corresponding zero-check (e.g., mismatched guards between the V1 and V2 window-size code paths, or interaction between `hardenResourceCalculation`/`allowHardenResourceCalculation` feature flags and the legacy math), an integer division by zero throws an uncaught `ArithmeticException` during block application in `Manager`, since this code executes as part of actuator `execute()`/native contract execution rather than inside a caught validation block. An uncaught exception during block application can crash or halt a witness node processing that block, a denial-of-service impact consistent with "node crash or halt."

### Likelihood Explanation
I could not fully verify, within the scope of this investigation, a concrete transaction sequence that drives `newOwnerUsage`, `newUsage`, or `windowSize` to exactly zero at the specific unguarded division sites (as opposed to the sites that already have `== 0` guards). The guards present in most call sites (`newOwnerUsage == 0` return, `remainUsage == 0` return) suggest the developers were aware of this exact zero-division risk and patched several of the call sites, but not uniformly across `increase()`, `increaseV2()`, and `getNewWindowSize()` in both `ResourceProcessor.java` and its duplicated logic in `RepositoryImpl.java`. This inconsistency is a code smell strongly analogous to the CVE's root cause (an unguarded division derived from untrusted/derived size data), but without tracing every legacy-vs-harden combination and every V1/V2 windowSize field state, I cannot confirm this is definitively exploitable end-to-end.

### Recommendation
Add explicit zero-checks (returning a safe default such as `this.windowSize` or 0) immediately before every division by `newUsage`, `newOwnerUsage`, `windowSize`, and `oldWindowSize` in `ResourceProcessor.java` and the duplicated logic in `RepositoryImpl.java`, and add regression tests that exercise delegate/undelegate resource flows with fully-consumed usage windows (usage reduced to exactly zero) under both the legacy and `allowHardenResourceCalculation` code paths, and both V1 and V2 (`supportAllowCancelAllUnfreezeV2`) window-size representations.

### Proof of Concept
I was unable to construct a concrete, verified transaction sequence within this investigation that forces `newOwnerUsage`/`newUsage` to zero at the unguarded division sites — this would require deeper tracing of `AccountCapsule.getWindowSize`/`getWindowSizeV2` state transitions across freeze/unfreeze/delegate/undelegate sequences than was possible here. I recommend a Devin session with full repository and test-execution access to attempt to drive these divisors to zero via a sequence of `FreezeBalanceV2`, `DelegateResource`, and `UnDelegateResource` transactions (and their TVM precompile equivalents) to confirm or refute exploitability before treating this as a confirmed vulnerability.

### Citations

**File:** chainbase/src/main/java/org/tron/core/db/ResourceProcessor.java (L163-183)
```java
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
```

**File:** chainbase/src/main/java/org/tron/core/db/ResourceProcessor.java (L206-217)
```java
    long newOwnerUsage = ownerUsage + transferUsage;
    // mean ownerUsage == 0 and transferUsage == 0
    if (newOwnerUsage == 0) {
      owner.setNewWindowSize(resourceCode, this.windowSize);
      owner.setUsage(resourceCode, 0);
      owner.setLatestTime(resourceCode, now);
      return;
    }
    // calculate new windowSize
    long newOwnerWindowSize = getNewWindowSize(ownerUsage, remainOwnerWindowSize, transferUsage,
        remainReceiverWindowSize, newOwnerUsage);
    owner.setNewWindowSize(resourceCode, newOwnerWindowSize);
```

**File:** chainbase/src/main/java/org/tron/core/db/ResourceProcessor.java (L262-270)
```java
  private long getNewWindowSize(long lastUsage, long lastWindowSize, long usage,
      long windowSize, long newUsage) {
    if (hardenCalculation()) {
      BigInteger bi = BigInteger.valueOf(lastUsage).multiply(BigInteger.valueOf(lastWindowSize))
          .add(BigInteger.valueOf(usage).multiply(BigInteger.valueOf(windowSize)));
      return bi.divide(BigInteger.valueOf(newUsage)).longValueExact();
    }
    return (lastUsage * lastWindowSize + usage * windowSize) / newUsage;
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/repository/RepositoryImpl.java (L965-986)
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

  private long getUsage(long usage, long windowSize) {
    if (hardenResourceCalculation()) {
      return BigInteger.valueOf(usage)
          .multiply(BigInteger.valueOf(windowSize))
          .divide(BigInteger.valueOf(precision))
          .longValueExact();
    }
    return usage * windowSize / precision;
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/UnDelegateResourceProcessor.java (L91-159)
```java
  public void execute(UnDelegateResourceParam param, Repository repo) {
    byte[] ownerAddress = param.getOwnerAddress();
    byte[] receiverAddress = param.getReceiverAddress();
    long unDelegateBalance = param.getUnDelegateBalance();
    AccountCapsule ownerCapsule = repo.getAccount(ownerAddress);
    AccountCapsule receiverCapsule = repo.getAccount(receiverAddress);
    DynamicPropertiesStore dynamicStore = repo.getDynamicPropertiesStore();
    long now = repo.getHeadSlot();

    long transferUsage = 0;
    // modify receiver Account
    if (receiverCapsule != null) {
      switch (param.getResourceType()) {
        case BANDWIDTH:
          BandwidthProcessor bandwidthProcessor = new BandwidthProcessor(ChainBaseManager.getInstance());
          bandwidthProcessor.updateUsageForDelegated(receiverCapsule);
          /* For example, in a scenario where a regular account can be upgraded to a contract
          account through an interface, the account information will be cleared after the
          contract suicide, and this account will be converted to a regular account in the future */
          if (receiverCapsule.getAcquiredDelegatedFrozenV2BalanceForBandwidth()
              < unDelegateBalance) {
            // A TVM contract suicide, re-create will produce this situation
            receiverCapsule.setAcquiredDelegatedFrozenV2BalanceForBandwidth(0);
          } else {
            // calculate usage
            long unDelegateMaxUsage = (long) ((double) unDelegateBalance / TRX_PRECISION
                * dynamicStore.getTotalNetLimit() / repo.getTotalNetWeight());
            transferUsage = (long) (receiverCapsule.getNetUsage()
                * ((double) (unDelegateBalance) / receiverCapsule.getAllFrozenBalanceForBandwidth()));
            transferUsage = min(unDelegateMaxUsage, transferUsage, VMConfig.disableJavaLangMath());

            receiverCapsule.addAcquiredDelegatedFrozenV2BalanceForBandwidth(-unDelegateBalance);
          }

          long newNetUsage = receiverCapsule.getNetUsage() - transferUsage;
          receiverCapsule.setNetUsage(newNetUsage);
          receiverCapsule.setLatestConsumeTime(now);
          break;
        case ENERGY:
          EnergyProcessor energyProcessor =
              new EnergyProcessor(dynamicStore, ChainBaseManager.getInstance().getAccountStore());
          energyProcessor.updateUsage(receiverCapsule);

          if (receiverCapsule.getAcquiredDelegatedFrozenV2BalanceForEnergy()
              < unDelegateBalance) {
            // A TVM contract receiver, re-create will produce this situation
            receiverCapsule.setAcquiredDelegatedFrozenV2BalanceForEnergy(0);
          } else {
            // calculate usage
            long unDelegateMaxUsage = (long) ((double) unDelegateBalance / TRX_PRECISION
                * dynamicStore.getTotalEnergyCurrentLimit() / repo.getTotalEnergyWeight());
            transferUsage = (long) (receiverCapsule.getEnergyUsage()
                * ((double) (unDelegateBalance) / receiverCapsule.getAllFrozenBalanceForEnergy()));
            transferUsage = min(unDelegateMaxUsage, transferUsage, VMConfig.disableJavaLangMath());

            receiverCapsule.addAcquiredDelegatedFrozenV2BalanceForEnergy(-unDelegateBalance);
          }

          long newEnergyUsage = receiverCapsule.getEnergyUsage() - transferUsage;
          receiverCapsule.setEnergyUsage(newEnergyUsage);
          receiverCapsule.setLatestConsumeTimeForEnergy(now);
          break;
        default:
          //this should never happen
          break;
      }
      repo.updateAccount(receiverCapsule.createDbKey(), receiverCapsule);
    }

```
