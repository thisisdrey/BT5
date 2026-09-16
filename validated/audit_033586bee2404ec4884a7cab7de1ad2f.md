This is my final iteration, so I'll synthesize what I found.

The strongest analog is a division-by-zero in `RepositoryImpl.calculateGlobalEnergyLimit`, used on the TVM contract-call path.

### Title
Divide-by-zero crash in `calculateGlobalEnergyLimit` when `totalEnergyWeight` is 0 (unfrozen network) - (File: `actuator/src/main/java/org/tron/core/vm/repository/RepositoryImpl.java`)

### Summary
`RepositoryImpl.calculateGlobalEnergyLimit` divides `totalEnergyLimit` by `totalEnergyWeight` without a runtime zero-check, relying only on a Java `assert` statement, which is disabled by default in production JVMs (`-ea` not set). [1](#0-0) 

### Finding Description
When TVM code paths (e.g. `TriggerSmartContract`, contract creation) need to determine a caller's energy limit, `RepositoryImpl.calculateGlobalEnergyLimit` computes `totalEnergyLimit / totalEnergyWeight`, guarded only by `assert totalEnergyWeight > 0;`. [2](#0-1)  Java assertions are disabled unless the JVM is started with `-ea`, which is not the default for production node deployments, so this check provides no actual protection at runtime. `totalEnergyWeight` is a global dynamic property maintained via `addTotalEnergyWeight` calls triggered by ordinary, unprivileged `FreezeBalance`/`UnfreezeBalance`/`UnDelegateResource` operations, e.g. in `UnfreezeBalanceActuator.execute` which calls `dynamicStore.addTotalEnergyWeight(weight)` with a computed `weight` that can drive the aggregate down to zero if the network's energy-frozen balance is fully unwound (e.g., on a low-liquidity test/private/sidechain network, or transiently during migrations). [3](#0-2)  Note that the equivalent method in `EnergyProcessor` (`calculateGlobalEnergyLimit`/`calculateGlobalEnergyLimitV2`) does have an explicit `if (totalEnergyWeight <= 0) return 0;` guard, showing the intended safe behavior that `RepositoryImpl`'s copy of the calculation omits. [4](#0-3) 

### Impact Explanation
If `totalEnergyWeight` reaches zero and any code path invokes `RepositoryImpl.calculateGlobalEnergyLimit` (reachable from contract execution/estimation via the VM `Repository` interface used by `VMActuator`), it throws an unhandled `ArithmeticException` (integer division) or produces `Infinity`/`NaN` cast to a garbage `long` (in the non-hardened double-math branch), which can crash the node processing the transaction or corrupt an account's computed energy limit. [5](#0-4) 

### Likelihood Explanation
I could not fully confirm from the index whether `totalEnergyWeight` can realistically reach exactly 0 on mainnet under `allowNewReward`/hardened-calculation configurations, since `EnergyProcessor`'s parallel implementation already guards this exact case, suggesting the developers were aware of and intentionally patched this scenario elsewhere but arguably missed doing so consistently in `RepositoryImpl`. Given the guard exists in one twin implementation but not the other, this looks like an inconsistency/regression rather than a demonstrated, currently-exploitable path on a live network with existing frozen balances — additional verification of `VMActuator`/`ContractState` call sites and whether `totalEnergyWeight` can concretely hit zero in a production chain state would be needed to confirm reachability.

### Recommendation
Add the same `totalEnergyWeight <= 0 → return 0` (or throw a checked exception) guard used in `EnergyProcessor.calculateGlobalEnergyLimitV2` to `RepositoryImpl.calculateGlobalEnergyLimit`, and replace the `assert` with a real runtime check, since assertions are not guaranteed to run in production.

### Proof of Concept
Not fully constructible from the indexed code alone — reaching `totalEnergyWeight == 0` requires driving the network's total frozen-for-energy balance to zero (via repeated `UnfreezeBalance`/`UnDelegateResource` actions) and then triggering a smart contract call that routes through `RepositoryImpl.calculateGlobalEnergyLimit`; the exact call chain from `VMActuator`/`ContractState` into this method was not fully traced within the available tool budget. A Devin session with full repo access and the ability to run the test suite (e.g. extending `RepositoryImplHardenTest`) would be needed to confirm exploitability and produce a concrete PoC.

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

**File:** actuator/src/main/java/org/tron/core/actuator/UnfreezeBalanceActuator.java (L243-252)
```java
    long weight = dynamicStore.allowNewReward() ? decrease : -unfreezeBalance / TRX_PRECISION;
    switch (unfreezeBalanceContract.getResource()) {
      case BANDWIDTH:
        dynamicStore
            .addTotalNetWeight(weight);
        break;
      case ENERGY:
        dynamicStore
            .addTotalEnergyWeight(weight);
        break;
```

**File:** chainbase/src/main/java/org/tron/core/db/EnergyProcessor.java (L154-166)
```java
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
