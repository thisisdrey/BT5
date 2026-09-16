### Title
Unguarded division by `totalEnergyWeight`/`totalNetWeight` in resource-limit calculations can throw uncaught `ArithmeticException`, freezing energy/bandwidth accounting for every transaction that touches the affected account - ([File: actuator/src/main/java/org/tron/core/vm/repository/RepositoryImpl.java])

### Summary
The Perennial report shows that a zero-weight market makes a required denominator zero, causing a revert that permanently bricks fund redemption. The same class of bug exists in java-tron's resource-weight math: `RepositoryImpl.calculateGlobalEnergyLimit` divides `totalEnergyLimit` by `totalEnergyWeight` with only a Java `assert` (disabled by default in production) guarding against zero, unlike its sibling implementations in `EnergyProcessor`/`BandwidthProcessor` which explicitly short-circuit on a zero weight.

### Finding Description
`RepositoryImpl.calculateGlobalEnergyLimit` (used by the TVM/actuator path for energy accounting during contract execution) computes: [1](#0-0) 

Unlike the equivalent method in `EnergyProcessor`, which explicitly returns `0` when `totalEnergyWeight <= 0` under the `allowNewReward()` flag before doing any division: [2](#0-1) 

`RepositoryImpl.calculateGlobalEnergyLimit` only has `assert totalEnergyWeight > 0;` as a safeguard, which is a no-op unless the JVM is started with `-ea` (assertions are disabled by default in production JVMs). If `hardenResourceCalculation()` (`VMConfig.allowHardenResourceCalculation()`) is enabled and `totalEnergyWeight` is ever `0`, the code falls through to `BigInteger.valueOf(...).divide(BigInteger.valueOf(totalEnergyWeight))`, which throws `java.lang.ArithmeticException: BigInteger divide by zero`. This is analogous to the reported Solidity bug: a legitimate/expected zero denominator state (here, `totalEnergyWeight == 0`) is not defensively handled in this particular code path, even though the sibling, non-VM path (`EnergyProcessor`) was hardened against exactly this condition.

This method is reachable from `ContractState.calculateGlobalEnergyLimit`, which simply delegates to the underlying `Repository`: [3](#0-2) 

`ContractState` is the `Repository` implementation used throughout the TVM (`Program`/`Runtime`) during ordinary smart-contract calls, meaning any transaction that triggers a contract call and needs to check/consume energy exercises this arithmetic.

### Impact Explanation
If `totalEnergyWeight` reaches zero (e.g., transiently, or via some accounting edge case in `FreezeBalanceActuator`/`UnfreezeBalanceActuator`'s `addTotalWeight`/`addTotalEnergyWeight` logic, which independently tracks weight deltas that could momentarily net to zero) while hardened resource calculation is active, any subsequent contract-call transaction that needs its energy limit computed via this specific `RepositoryImpl` path throws an uncaught `ArithmeticException`. Depending on where this exception propagates (inside `Runtime`/`VMActuator`'s transaction execution, versus being caught generically), this can manifest as a node crash during block application or a transaction that can never be successfully executed - effectively "bricking" the ability to process transactions/contract calls, which matches the reported bug class of unhandled division-by-zero causing definitive loss of function.

### Likelihood Explanation
Likelihood is **uncertain/unconfirmed** given my available tooling: I was not able to fully trace (a) whether `totalEnergyWeight` can concretely reach exactly `0` while contract calls needing this exact `RepositoryImpl` path are still occurring, and (b) whether the `ArithmeticException` is caught somewhere higher up the `VMActuator`/`Runtime` call stack (which would downgrade this to a per-transaction failure rather than a node crash). The inconsistency between `RepositoryImpl` (assert-only guard) and `EnergyProcessor`/`BandwidthProcessor` (explicit zero-check) is a genuine code smell and a plausible root cause, but I could not fully verify end-to-end exploitability with the tools available in this session.

### Recommendation
Add the same explicit `totalEnergyWeight <= 0` (and analogous `totalNetWeight`/`totalTronPowerWeight`) short-circuit guards to `RepositoryImpl.calculateGlobalEnergyLimit` and any other TVM-path resource calculations that currently rely only on the disabled `assert` statement, mirroring the guards already present in `EnergyProcessor.calculateGlobalEnergyLimit` and `BandwidthProcessor.calculateGlobalNetLimit`.

### Proof of Concept
Not independently reproducible with the tools in this session — could not confirm a concrete transaction sequence that drives `totalEnergyWeight` to exactly `0` while `hardenResourceCalculation()` is enabled and a contract-call transaction is in flight through `ContractState`/`RepositoryImpl`. A background Devin session with full repo/build access would be needed to trace `VMActuator`/`Runtime` exception handling and construct a concrete unit-test reproduction.

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
