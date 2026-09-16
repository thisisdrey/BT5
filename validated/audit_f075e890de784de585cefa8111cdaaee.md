### Title
Unguarded `assert` instead of runtime check lets `RepositoryImpl.calculateGlobalEnergyLimit` divide by a zero `totalEnergyWeight`, crashing TVM energy-limit calculation for any contract call - (File: `actuator/src/main/java/org/tron/core/vm/repository/RepositoryImpl.java`)

### Summary
`RepositoryImpl.calculateGlobalEnergyLimit(AccountCapsule)` computes an account's energy limit by dividing by the global `totalEnergyWeight` value read from `DynamicPropertiesStore`. Unlike its sibling implementation in `EnergyProcessor.calculateGlobalEnergyLimit`, which explicitly returns `0` when `totalEnergyWeight <= 0`, the `RepositoryImpl` version only guards this invariant with a Java `assert` statement, which is a runtime no-op unless the JVM is started with `-ea` (not the default for production nodes). This is directly analogous to the reported MerkleResistor bug, where an unguarded rate value could be zero and later used as a divisor, causing a revert that bricks the feature — here the same pattern (compute a chain-wide weight that can legitimately be driven to zero by ordinary user unfreeze operations, then divide by it without a runtime check) causes an uncaught `ArithmeticException` ("BigInteger divide by zero") in the hardened path.

### Finding Description [1](#0-0) 

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

Compare this to the guarded twin implementation in `EnergyProcessor`, which correctly short-circuits when the weight is non-positive: [2](#0-1) 

```java
public long calculateGlobalEnergyLimit(AccountCapsule accountCapsule) {
    ...
    long totalEnergyWeight = dynamicPropertiesStore.getTotalEnergyWeight();
    if (dynamicPropertiesStore.allowNewReward() && totalEnergyWeight <= 0) {
      return 0;
    } else {
      assert totalEnergyWeight > 0;
    }
    if (hardenCalculation()) {
      return calculateGlobalLimitV1(frozeBalance, totalEnergyLimit, totalEnergyWeight);
    }
    ...
}
```

and to `EnergyProcessor.calculateGlobalEnergyLimitV2`/`BandwidthProcessor.calculateGlobalNetLimitV2`, which both perform an explicit `if (totalEnergyWeight == 0) { return 0; }` / `if (totalNetWeight == 0) { return 0; }` check before dividing: [3](#0-2) [4](#0-3) 

The `RepositoryImpl` path is reached from any transaction that triggers TVM contract execution, via `VMActuator.getAccountEnergyLimitWithFloatRatio`: [5](#0-4) 

```java
long totalEnergyFromFreeze = rootRepository
    .calculateGlobalEnergyLimit(account);
long leftBalanceForEnergyFreeze = getEnergyFee(totalBalanceForEnergyFreeze,
    leftEnergyFromFreeze,
    totalEnergyFromFreeze);
```

`hardenResourceCalculation()` gates use of the `BigInteger` division path, and is controlled by `VMConfig.allowHardenResourceCalculation()`, an on-chain-governable proposal parameter — i.e. once the hardened calculation is enabled (as new java-tron networks are moving to for overflow safety, per `CalculateGlobalLimitHardenTest`), the assert-only guard becomes the sole protection against a zero divisor, and it is compiled away in production JVMs that run without `-ea`.

### Impact Explanation
If `totalEnergyWeight` (a globally-tracked, on-chain-persisted counter updated by every freeze/unfreeze/delegate/undelegate transaction) is ever zero or becomes zero through normal user operations (all frozen-for-energy balance withdrawn network-wide, or via edge-case rounding drift across many independent unfreeze weight-decrement computations), any subsequent TVM contract call from any unprivileged account that reaches this code path with the hardened calculation enabled throws an uncaught `ArithmeticException` from `BigInteger.divide`. This turns a legitimate resource-accounting computation into a crash/revert path for contract execution — a denial-of-service on the TVM execution/energy-accounting pipeline, matching the "division-by-zero bricking core functionality" class of the referenced report.

### Likelihood Explanation
The condition is reachable purely through unprivileged, everyday user actions (freeze/unfreeze energy resources) that cumulatively drive the network's `totalEnergyWeight` counter, and the vulnerable code executes on the hot path of every smart-contract-invoking transaction once hardened resource calculation is active. The bug's likelihood is elevated by the fact that this exact invariant is *already known to be fragile* elsewhere in the same codebase (evidenced by explicit `== 0` runtime guards added in the sibling `EnergyProcessor`/`BandwidthProcessor` V2 methods and the extensive `CalculateGlobalLimitHardenTest` suite covering overflow/zero-weight scenarios) but was not consistently applied to this method.

### Recommendation
Replace the `assert totalEnergyWeight > 0;` with an explicit runtime guard consistent with the sibling implementations, e.g.:
```java
if (totalEnergyWeight <= 0) {
  return 0;
}
```
before entering either the `hardenResourceCalculation()` BigInteger branch or the legacy double-arithmetic branch, mirroring the guards already present in `EnergyProcessor.calculateGlobalEnergyLimit`/`calculateGlobalEnergyLimitV2` and `BandwidthProcessor.calculateGlobalNetLimit`/`calculateGlobalNetLimitV2`.

### Proof of Concept
1. Deploy/observe a network state where `DynamicPropertiesStore.getTotalEnergyWeight()` returns `0` (e.g., freshly bootstrapped chain before any account freezes TRX for energy, or a private/test network after all energy-frozen balances have been unfrozen) while `VMConfig.allowHardenResourceCalculation()` is enabled via governance proposal.
2. Have any account with `getAllFrozenBalanceForEnergy() >= TRX_PRECISION` (from stale/pre-existing state) invoke a smart contract, driving execution into `VMActuator.getAccountEnergyLimitWithFloatRatio` → `RepositoryImpl.calculateGlobalEnergyLimit`.
3. The call `BigInteger.valueOf(totalEnergyLimit).divide(BigInteger.valueOf(0))` throws `ArithmeticException: BigInteger divide by zero`, which is not caught anywhere in this call chain, propagating out of the TVM energy-limit computation for that transaction.

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

**File:** chainbase/src/main/java/org/tron/core/db/BandwidthProcessor.java (L455-466)
```java
  public long calculateGlobalNetLimitV2(long frozeBalance) {
    long totalNetLimit = dynamicPropertiesStore.getTotalNetLimit();
    long totalNetWeight = dynamicPropertiesStore.getTotalNetWeight();
    if (totalNetWeight == 0) {
      return 0;
    }
    if (hardenCalculation()) {
      return calculateGlobalLimitV2(frozeBalance, totalNetLimit, totalNetWeight);
    }
    double netWeight = (double) frozeBalance / TRX_PRECISION;
    return (long) (netWeight * ((double) totalNetLimit / totalNetWeight));
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/VMActuator.java (L639-650)
```java
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

```
