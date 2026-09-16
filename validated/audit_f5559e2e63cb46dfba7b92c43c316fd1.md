Confirmed: `RepositoryImpl.calculateGlobalEnergyLimit()` in `actuator/src/main/java/org/tron/core/vm/repository/RepositoryImpl.java` never checks `supportUnfreezeDelay()` and has no V2 path at all, while `EnergyProcessor.calculateGlobalEnergyLimit()` in `chainbase/src/main/java/org/tron/core/db/EnergyProcessor.java` does — routing to `calculateGlobalEnergyLimitV2()` when the flag is on, and also gating on `allowNewReward()`.

### Title
Divergent duplicate implementations of `calculateGlobalEnergyLimit()` between TVM `RepositoryImpl` and `EnergyProcessor` - (File: actuator/src/main/java/org/tron/core/vm/repository/RepositoryImpl.java)

### Summary
This is the java-tron analog of the reported `getBidValue()` issue: a value-computing function that is expected to represent one canonical concept (an account's total energy limit derived from frozen balance) is implemented independently in two places instead of being shared, and one implementation has since diverged from the other by missing a feature/guard added to the other.

### Finding Description
`RepositoryImpl.calculateGlobalEnergyLimit(AccountCapsule)` [1](#0-0)  always uses the legacy V1 formula (`frozeBalance / TRX_PRECISION` weighted against `totalEnergyWeight`), with no branch for `dynamicPropertiesStore.supportUnfreezeDelay()` and no `allowNewReward()` zero-weight guard.

`EnergyProcessor.calculateGlobalEnergyLimit(AccountCapsule)` [2](#0-1)  implements the same concept but checks `supportUnfreezeDelay()` and delegates to `calculateGlobalEnergyLimitV2(frozeBalance)` [3](#0-2) , which uses a double-precision `energyWeight` (no integer truncation via `/ TRX_PRECISION`) and a different hardened calculation path (`calculateGlobalLimitV2`), plus it returns `0` early if `allowNewReward()` is set and `totalEnergyWeight <= 0`.

`RepositoryImpl.calculateGlobalEnergyLimit()` is the function that backs energy-limit computation for TVM contract calls, reachable from `getAccountLeftEnergyFromFreeze()` [4](#0-3)  and directly from `VMActuator.getAccountEnergyLimitWithFloatRatio()` [5](#0-4) , both of which gate the energy a caller/creator receives when triggering a smart contract (`TriggerSmartContract`). `EnergyProcessor.calculateGlobalEnergyLimit()`/`getAccountLeftEnergyFromFreeze()` back the general resource accounting path used by `useEnergy()` [6](#0-5) .

Once `supportUnfreezeDelay` (the "new freezing model"/unfreeze-delay feature, referenced across `BandwidthProcessor`, `FreezeBalanceV2Actuator`, `UnfreezeBalanceV2Actuator`, `WithdrawExpireUnfreezeActuator`, `DynamicPropertiesStore`) is enabled on-chain, the two code paths that are conceptually supposed to compute "how much energy an account is entitled to based on frozen balance" will produce different numbers for the same account state: TVM smart-contract execution keeps using the old V1 integer-truncated formula, while ordinary resource accounting uses the new V2 double-precision formula with the `allowNewReward` guard.

### Impact Explanation
Any account that freezes TRX for energy and then triggers a smart contract is affected, since the energy limit computed for TVM execution no longer matches the network's canonical resource accounting once `supportUnfreezeDelay` is active. Depending on the direction of the discrepancy (integer truncation vs. double precision, and the missing `allowNewReward` zero-weight short-circuit), an account could either receive more energy for contract execution than its frozen balance should back — effectively unbacked/free TVM computation, harming network fairness and potentially enabling resource-exhaustion abuse funded by less TRX than intended — or, less severely, be denied energy it should be entitled to. This falls squarely within the analog's rule set (TVM energy metering / stake-derived resource math reachable from any signed `TriggerSmartContract` transaction).

### Likelihood Explanation
Reaching this divergence requires only that the network has activated `supportUnfreezeDelay` (a hard-fork parameter, not attacker-controlled) and that a caller freezes TRX for energy then invokes a contract — no special privilege, malicious SR, or off-path node condition is needed, so it is reachable by any ordinary transaction broadcaster once the feature is live. I could not fully verify from source alone whether `supportUnfreezeDelay` is currently activated on mainnet/testnet, nor precisely quantify the numeric direction/magnitude of the resulting discrepancy between V1 and V2 formulas for concrete frozen-balance values — this would require exercising both code paths with the flag toggled, similar to the existing `CalculateGlobalLimitHardenTest`/`RepositoryImplHardenTest` test suites which currently only test hardening (BigInteger vs double) parity, not the `supportUnfreezeDelay` V1/V2 branch divergence.

### Recommendation
Make `RepositoryImpl.calculateGlobalEnergyLimit()` delegate to (or reimplement identically) the same `supportUnfreezeDelay`-aware logic as `EnergyProcessor.calculateGlobalEnergyLimit()`/`calculateGlobalEnergyLimitV2()`, including the `allowNewReward()` guard, so that TVM energy-limit computation and general resource accounting always agree on the same account's entitled energy. Alternatively, factor the shared logic into one place (e.g. `ResourceProcessor` or a shared utility) that both `RepositoryImpl` and `EnergyProcessor` call, eliminating the duplicate implementation entirely — the same remediation approach the report's Fastlane fix used (removing the duplicate call rather than keeping two divergent copies).

### Proof of Concept
1. On a network/test chain with `supportUnfreezeDelay` enabled via `DynamicPropertiesStore`, freeze TRX for energy on an account.
2. Compute the account's energy entitlement via `EnergyProcessor.calculateGlobalEnergyLimit(account)` (used by `useEnergy`) — this takes the V2, double-precision branch.
3. Separately compute the entitlement via `RepositoryImpl.calculateGlobalEnergyLimit(account)` (used by `VMActuator`/`getAccountLeftEnergyFromFreeze` when the same account triggers a smart contract) — this always takes the legacy V1 integer-truncated branch.
4. Compare the two results for the same frozen balance/`totalEnergyWeight` inputs; for non-trivial values they will differ (V1 truncates `frozeBalance / TRX_PRECISION` to a long before weighting, V2 keeps it as `double`), demonstrating that TVM contract-call energy limits and the account's canonical resource-model energy limit diverge under the same on-chain state. Precise numeric divergence for specific balances was not run interactively here and would need to be confirmed by instrumenting both methods with identical inputs.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/repository/RepositoryImpl.java (L186-198)
```java
  public long getAccountLeftEnergyFromFreeze(AccountCapsule accountCapsule) {
    long now = getHeadSlot();

    long energyUsage = accountCapsule.getEnergyUsage();
    long latestConsumeTime = accountCapsule.getAccountResource().getLatestConsumeTimeForEnergy();
    long energyLimit = calculateGlobalEnergyLimit(accountCapsule);

    long windowSize = accountCapsule.getWindowSize(Common.ResourceCode.ENERGY);

    long newEnergyUsage = recover(energyUsage, latestConsumeTime, now, windowSize);

    return max(energyLimit - newEnergyUsage, 0, VMConfig.disableJavaLangMath()); // us
  }
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

**File:** chainbase/src/main/java/org/tron/core/db/EnergyProcessor.java (L145-166)
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
```

**File:** chainbase/src/main/java/org/tron/core/db/EnergyProcessor.java (L168-179)
```java
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
```

**File:** actuator/src/main/java/org/tron/core/actuator/VMActuator.java (L625-664)
```java
  private long getAccountEnergyLimitWithFloatRatio(AccountCapsule account, long feeLimit,
      long callValue) {

    long sunPerEnergy = VMConstant.SUN_PER_ENERGY;
    if (rootRepository.getDynamicPropertiesStore().getEnergyFee() > 0) {
      sunPerEnergy = rootRepository.getDynamicPropertiesStore().getEnergyFee();
    }
    // can change the calc way
    long leftEnergyFromFreeze = rootRepository.getAccountLeftEnergyFromFreeze(account);
    callValue = max(callValue, 0, VMConfig.disableJavaLangMath());
    long energyFromBalance = floorDiv(max(
        account.getBalance() - callValue, 0, VMConfig.disableJavaLangMath()), sunPerEnergy,
        VMConfig.disableJavaLangMath());

    long energyFromFeeLimit;
    long totalBalanceForEnergyFreeze = account.getAllFrozenBalanceForEnergy();
    if (0 == totalBalanceForEnergyFreeze) {
      energyFromFeeLimit =
          feeLimit / sunPerEnergy;
    } else {
      long totalEnergyFromFreeze = rootRepository
          .calculateGlobalEnergyLimit(account);
      long leftBalanceForEnergyFreeze = getEnergyFee(totalBalanceForEnergyFreeze,
          leftEnergyFromFreeze,
          totalEnergyFromFreeze);

      if (leftBalanceForEnergyFreeze >= feeLimit) {
        energyFromFeeLimit = BigInteger.valueOf(totalEnergyFromFreeze)
            .multiply(BigInteger.valueOf(feeLimit))
            .divide(BigInteger.valueOf(totalBalanceForEnergyFreeze)).longValueExact();
      } else {
        energyFromFeeLimit = addExact(
            leftEnergyFromFreeze, (feeLimit - leftBalanceForEnergyFreeze) / sunPerEnergy,
            VMConfig.disableJavaLangMath());
      }
    }

    return min(addExact(leftEnergyFromFreeze, energyFromBalance,
            VMConfig.disableJavaLangMath()), energyFromFeeLimit, VMConfig.disableJavaLangMath());
  }
```
