### Title
Unguarded division by `totalEnergyWeight` in energy-limit calculation can throw an uncaught `ArithmeticException` and halt block/transaction processing - (File: chainbase/src/main/java/org/tron/core/db/EnergyProcessor.java)

### Summary
The ImageMagick advisory (JLSEC-2026-879) describes an unguarded integer division in `coders/jp2.c` that can divide by zero and crash the process. The closest reachable analog in java-tron is `EnergyProcessor.calculateGlobalEnergyLimit`/`calculateGlobalEnergyLimitV2` and their `RepositoryImpl` counterpart, where the only protection against `totalEnergyWeight == 0` is a Java `assert` statement — a construct that is compiled out and does nothing at runtime unless the JVM is started with `-ea`, which production java-tron nodes are not.

### Finding Description
`EnergyProcessor.calculateGlobalEnergyLimit` reads the chain-wide `totalEnergyWeight` and, if the new-reward gate is not active, relies solely on `assert totalEnergyWeight > 0;` before dividing by it: [1](#0-0) 

`RepositoryImpl.calculateGlobalEnergyLimit` (used by the TVM/actuator repository layer) has the exact same pattern, again gated only by `assert`: [2](#0-1) 

In production, Java assertions are disabled by default, so `assert totalEnergyWeight > 0;` is a no-op. If `totalEnergyWeight` is ever `0` (or negative) when this code executes with the non-hardened (`hardenCalculation()`/`hardenResourceCalculation()` disabled) path, the expression `(double) totalEnergyLimit / totalEnergyWeight` silently produces `Infinity`/`NaN`, and the hardened `BigInteger` path (`.divide(BigInteger.valueOf(totalEnergyWeight))`) throws an uncaught `ArithmeticException: / by zero`.

This method is invoked from `EnergyProcessor.useEnergy`, which is on the hot path for every TVM contract call (`TriggerSmartContract`) energy accounting during transaction execution: [3](#0-2) 

There is no `try/catch` around `calculateGlobalEnergyLimit()` in `useEnergy`, so an `ArithmeticException` here propagates up through actuator execution and, ultimately, block application in `Manager`.

### Impact Explanation
If `totalEnergyWeight` reaches zero (e.g., on a young/low-stake network, a private/test chain, or after a mass-unfreeze event drives the chain-wide energy weight to zero), the very next TVM contract-call transaction that reaches energy accounting will throw an uncaught `ArithmeticException` in the hardened path, or silently compute a bogus `Long.MAX_VALUE`-scale energy allowance in the legacy double-arithmetic path. The former is a node crash/consensus-halt risk during block/transaction processing (an unprivileged contract caller can trigger it merely by calling any contract), and the latter is an unbacked-resource condition (an account is granted effectively unlimited energy, letting it execute unmetered computation for free — abusable for a compute/DoS attack against nodes). Both outcomes fall within the accepted impact categories (node crash or halt / unbacked resource grant enabling DoS).

### Likelihood Explanation
Reaching `totalEnergyWeight == 0` on TRON mainnet under normal conditions is unlikely because there is generally always some frozen TRX contributing to it. However, the code contains **no defensive runtime check** — the `assert` gives a false sense of safety while providing none in production builds — so on any network configuration where `totalEnergyWeight` can transiently reach zero (new networks, sidechains, private/consortium deployments, or extreme unfreeze scenarios), a single ordinary `TriggerSmartContract` transaction from any unprivileged account is sufficient to trigger the divide-by-zero path. This mirrors the ImageMagick class of bug: an input condition (here, chain state) that is assumed-but-not-enforced to be non-zero.

### Recommendation
Replace the `assert totalEnergyWeight > 0;` checks in `EnergyProcessor.calculateGlobalEnergyLimit` and `RepositoryImpl.calculateGlobalEnergyLimit` with real runtime guards (e.g., `if (totalEnergyWeight <= 0) { return 0; }`), consistent with the pattern already used in `calculateGlobalEnergyLimitV2` (`if (totalEnergyWeight == 0) { return 0; }`), and apply the same fix symmetrically to the analogous bandwidth/net-weight calculations if they share this pattern.

### Proof of Concept
Not independently reproducible from the index alone — reaching `totalEnergyWeight == 0` requires a specific chain-state precondition (either a freshly bootstrapped/low-stake network or a scenario where the running total of energy weight is driven to zero via unfreeze operations) that could not be fully verified from the available code alone. What is concretely verifiable is the code path itself: `EnergyProcessor.useEnergy` → `calculateGlobalEnergyLimit` → (assert no-op in production) → division by `totalEnergyWeight`, entered on every TVM contract-call transaction that consumes energy, with no surrounding exception handling. [4](#0-3)

### Citations

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
