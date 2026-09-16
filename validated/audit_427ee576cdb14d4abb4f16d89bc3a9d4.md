## Title
Division by zero in `RepositoryImpl.calculateGlobalEnergyLimit` when `TotalEnergyWeight` is zero, unguarded except by a disabled Java `assert` - ([File: actuator/src/main/java/org/tron/core/vm/repository/RepositoryImpl.java])

### Summary
The TVM energy-limit calculation used on every contract execution path relies on a Java `assert` statement to guard against a zero divisor. Java assertions are disabled by default in production JVMs (they require the `-ea` flag), so if `TotalEnergyWeight` is ever `0`, the subsequent `BigInteger` division throws an unhandled `ArithmeticException`, aborting the transaction execution path.

### Finding Description
`RepositoryImpl.calculateGlobalEnergyLimit` computes the energy limit for a contract account based on `TotalEnergyWeight`: [1](#0-0) 

The only protection against `totalEnergyWeight == 0` is `assert totalEnergyWeight > 0;`, which is a no-op unless the JVM is started with `-ea`. If the assertion is disabled (the default), execution proceeds to:
```java
BigInteger.valueOf(energyWeight)
    .multiply(BigInteger.valueOf(totalEnergyLimit))
    .divide(BigInteger.valueOf(totalEnergyWeight))   // throws ArithmeticException when totalEnergyWeight == 0
    .longValueExact();
```
when `hardenResourceCalculation()` (`VMConfig.allowHardenResourceCalculation()`) is enabled, causing a `BigInteger` divide-by-zero `ArithmeticException`.

This method is invoked via `EnergyProcessor`-style resource accounting from `useEnergy`-equivalent logic during TVM contract calls, so it is reachable by an ordinary account issuing a `TriggerSmartContract` transaction against any deployed contract, without needing SR/witness privileges.

The chainbase `EnergyProcessor.calculateGlobalEnergyLimit` variant has an explicit runtime check (`if (dynamicPropertiesStore.allowNewReward() && totalEnergyWeight <= 0) return 0;`) that is real production logic rather than an assertion: [2](#0-1) 

But `RepositoryImpl`'s copy of the same logic omits this runtime guard entirely and depends solely on the disabled `assert`, making it the weaker/unguarded duplicate implementation.

### Impact Explanation
If `TotalEnergyWeight` reaches zero (e.g., through mass unfreeze/undelegate of all energy-frozen balances network-wide, or during early-chain/testnet conditions before any energy is frozen) and `allowHardenResourceCalculation()` is active, every subsequent contract-triggering transaction that reaches this code path throws an uncaught `ArithmeticException`. Because assertions are off by default, there is no early, controlled rejection — the exception propagates from deep inside resource accounting, which can disrupt block/transaction processing for TVM calls, a form of denial of service in the contract-execution/energy-metering path.

### Likelihood Explanation
The likelihood depends on how easily `TotalEnergyWeight` can hit exactly zero in a live network — on a long-running mainnet this is unlikely because plenty of accounts keep energy frozen, but it is more plausible on private/test/consortium chains or immediately after genesis before any freeze operations occur, and the guard is structurally weaker than the equivalent `EnergyProcessor` check since it relies only on a disabled `assert`.

### Recommendation
Replace the `assert totalEnergyWeight > 0;` in `RepositoryImpl.calculateGlobalEnergyLimit` with an explicit runtime check mirroring `EnergyProcessor.calculateGlobalEnergyLimit`, e.g.:
```java
if (totalEnergyWeight <= 0) {
    return 0;
}
```
before performing either the `BigInteger` or `double` based division, so the divide-by-zero condition can never be reached regardless of JVM assertion settings.

### Proof of Concept
1. Deploy/operate a chain (or reach a state) where `TotalEnergyWeight == 0` (e.g., a freshly initialized private network before any account freezes TRX for energy, or after all energy-frozen balances are fully unfrozen/undelegated network-wide).
2. Ensure `allowHardenResourceCalculation()` returns `true` (chain parameter enabling hardened resource math).
3. Any unprivileged account submits a `TriggerSmartContract` transaction invoking a deployed contract.
4. During energy accounting, `RepositoryImpl.calculateGlobalEnergyLimit` is invoked with `totalEnergyWeight = 0`; the disabled `assert` does not fire, and `BigInteger.divide(BigInteger.ZERO)` throws `ArithmeticException`, propagating out of the resource-accounting call and disrupting transaction/contract execution.

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
