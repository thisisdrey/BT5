### Title
Missing runtime check for `totalEnergyWeight == 0` before division in `RepositoryImpl::calculateGlobalEnergyLimit` causes `ArithmeticException` during TVM contract execution - ([File: actuator/src/main/java/org/tron/core/vm/repository/RepositoryImpl.java])

### Summary
`RepositoryImpl.calculateGlobalEnergyLimit` divides by `totalEnergyWeight` while only guarding the zero case with a Java `assert` statement, which is disabled by default in production JVMs (no `-ea` flag). This mirrors the reported bug class: a critical divisor (`_minSubmissionsRequired`/`contributorsN` in the original report, `totalEnergyWeight` here) is not defensively checked before being used as a denominator, so a legitimate, reachable state (zero total energy weight) can trigger an unhandled division error.

### Finding Description
In `calculateGlobalEnergyLimit`: [1](#0-0) 

the only protection against `totalEnergyWeight == 0` is `assert totalEnergyWeight > 0;`. Java assertions are stripped/disabled at runtime unless the JVM is started with `-ea`, which is not the default for production nodes. If `totalEnergyWeight` is `0` (e.g., freshly bootstrapped network, or a state where no account currently has energy frozen so `getTotalEnergyWeight()` returns 0), the subsequent `BigInteger... .divide(BigInteger.valueOf(totalEnergyWeight))` (when `hardenResourceCalculation()` is enabled) throws `ArithmeticException: / by zero`, and the non-hardened path `(double) totalEnergyLimit / totalEnergyWeight` produces `Infinity`/`NaN` silently corrupting downstream `long` casts instead of failing safely.

This differs from the sibling implementation in `EnergyProcessor.calculateGlobalEnergyLimit`, which contains an actual runtime guard: [2](#0-1) 
that returns `0` when `totalEnergyWeight <= 0` (guarded by `allowNewReward()`), showing the codebase is aware of this hazard elsewhere but the `RepositoryImpl` (VM/TVM) code path lacks the equivalent check.

`RepositoryImpl.calculateGlobalEnergyLimit` is exposed through the `Repository` interface and its delegate `ContractState.calculateGlobalEnergyLimit`: [3](#0-2) 
which is reachable from ordinary smart-contract execution paths (any `TriggerSmartContract` transaction that hits energy-limit computation for an account with frozen energy balance), making it reachable by any unprivileged transaction broadcaster.

### Impact Explanation
An uncaught `ArithmeticException` thrown mid-transaction execution inside TVM/native-contract processing can abort transaction processing unexpectedly (denial-of-service for that transaction path) or, in the non-hardened arithmetic branch, silently produce corrupted `Infinity`/`NaN`-derived energy limits that get cast to `long`, potentially resulting in incorrect energy accounting (unbounded energy limit or zero, i.e., unbacked resource allowance or improper resource throttling). Because the calculation feeds account energy limits used across the whole node's transaction execution, a systemic zero `totalEnergyWeight` state could cause repeated failures/halts across many transactions.

### Likelihood Explanation
Likelihood depends on `totalEnergyWeight` reaching zero on a live network, which is atypical for a mature chain but plausible on: a freshly initialized network before any freeze operations, or a hypothetical state where all frozen-for-energy balances are simultaneously unfrozen. Since this is a state-dependent edge case rather than something directly forced by a single attacker-controlled parameter (unlike the original `_minSubmissionsRequired` report where an admin misconfiguration sets a value to 0), likelihood is lower, but the missing defensive check is a direct structural analog of the reported issue.

### Recommendation
Replace the ineffective `assert totalEnergyWeight > 0;` in `RepositoryImpl.calculateGlobalEnergyLimit` with an explicit runtime check (matching the guard pattern already used in `EnergyProcessor.calculateGlobalEnergyLimit`), e.g., return `0` (or otherwise safely short-circuit) when `totalEnergyWeight <= 0`, before performing the division.

### Proof of Concept
1. Bring `totalEnergyWeight` (via `DynamicPropertiesStore`/`getTotalEnergyWeight()`) to `0` — reachable in a network state with no active energy-frozen balances, e.g. right after all accounts have fully unfrozen energy-frozen balances via `UnfreezeBalanceV2`/`UnDelegateResourceContract` paths (see calls in `UnDelegateResourceActuator`/`UnDelegateResourceProcessor` which mutate `totalEnergyWeight`): [4](#0-3) 
2. Have an account with `getAllFrozenBalanceForEnergy() >= TRX_PRECISION` trigger any smart contract (`TriggerSmartContract`) that internally calls `Repository.calculateGlobalEnergyLimit` (via `ContractState`).
3. With `allowHardenResourceCalculation()` enabled, the `BigInteger.divide(BigInteger.valueOf(0))` throws `ArithmeticException`; with it disabled, `(double) totalEnergyLimit / 0` yields `Infinity`, corrupting the returned `long` energy limit. Existing unit tests already demonstrate the equivalent unguarded overflow/zero-division behavior for the parity code paths, e.g.: [5](#0-4)

### Citations

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

**File:** actuator/src/main/java/org/tron/core/vm/program/ContractState.java (L287-290)
```java
  @Override
  public long calculateGlobalEnergyLimit(AccountCapsule accountCapsule) {
    return repository.calculateGlobalEnergyLimit(accountCapsule);
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/UnDelegateResourceProcessor.java (L129-147)
```java
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
