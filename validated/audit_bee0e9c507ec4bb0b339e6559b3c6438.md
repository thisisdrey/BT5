### Title
Division before multiplication in `unDelegateMaxUsage` calculation causes resource-usage transfer miscalculation - (File: `actuator/src/main/java/org/tron/core/vm/nativecontract/UnDelegateResourceProcessor.java`)

### Summary
The `execute` method of `UnDelegateResourceProcessor` (invoked by the TVM `unDelegateResource` native contract, reachable from any smart-contract call) and its wallet-side twin `UnDelegateResourceActuator` (invoked directly by an `UnDelegateResourceContract` transaction) compute the cap on transferable resource usage with the same anti-pattern flagged in the external report: division is performed before multiplication using floating-point `double` arithmetic, instead of multiplying first (or using integer/BigInteger arithmetic) and dividing last.

### Finding Description
In `UnDelegateResourceProcessor.execute`: [1](#0-0) 

```java
long unDelegateMaxUsage = (long) ((double) unDelegateBalance / TRX_PRECISION
    * dynamicStore.getTotalNetLimit() / repo.getTotalNetWeight());
transferUsage = (long) (receiverCapsule.getNetUsage()
    * ((double) (unDelegateBalance) / receiverCapsule.getAllFrozenBalanceForBandwidth()));
transferUsage = min(unDelegateMaxUsage, transferUsage, VMConfig.disableJavaLangMath());
```

The same pattern is repeated for ENERGY in the same method: [2](#0-1) 

And identically in the wallet actuator path for both resource types: [3](#0-2) [4](#0-3) 

`unDelegateBalance / TRX_PRECISION` is evaluated first (dividing a comparatively small `long` cast to `double` by `1_000_000`), then multiplied by `totalNetLimit`, then divided by `totalNetWeight`. Because the first division truncates precision in the intermediate `double` representation before the subsequent multiplication scales it back up, the final result can diverge from the mathematically correct `unDelegateBalance * totalNetLimit / (TRX_PRECISION * totalNetWeight)`, exactly the class of bug identified in the external report (`_calculateMinRepayUnits` dividing before multiplying).

Notably, this same repository already recognizes and has fixed the identical class of bug elsewhere via "hardened" BigInteger multiply-then-divide implementations, e.g. `ResourceProcessor.calculateGlobalLimitV1/V2` and `RepositoryImpl.usageToBalance`/`calculateGlobalEnergyLimit`: [5](#0-4) [6](#0-5) 

However, the `unDelegateMaxUsage` computation in `UnDelegateResourceActuator`/`UnDelegateResourceProcessor` was never migrated to a hardened, multiply-before-divide (or BigInteger) form, leaving the same precision-loss defect live in the un-delegate resource path.

### Impact Explanation
`unDelegateMaxUsage` is used via `min(unDelegateMaxUsage, transferUsage, ...)` to cap how much bandwidth/energy usage is moved from the receiver back to the owner when a resource delegation is revoked. Precision loss in this bound can make the computed cap smaller or larger than the true proportional entitlement, causing incorrect usage accounting to be moved between the delegator and delegatee accounts on every `UnDelegateResourceContract` transaction or `unDelegateResource` TVM call. This directly perturbs bandwidth/energy usage bookkeeping (`NetUsage`/`EnergyUsage`) for both accounts, which can result in accounts retaining more free resource usage than they are entitled to (effectively unbacked resource allowance) or owners being shortchanged on usage restoration.

### Likelihood Explanation
This code path is reached by any account issuing a standard `UnDelegateResourceContract` transaction, and additionally by any deployed smart contract invoking the `unDelegateResource` native/precompiled contract — both are unprivileged, single-transaction operations requiring no special permissions, making the likelihood of triggering the flawed calculation high for any user who has delegated and un-delegates bandwidth/energy.

### Recommendation
Rewrite `unDelegateMaxUsage` to multiply before dividing, using `BigInteger` (matching the pattern already used in `ResourceProcessor.calculateGlobalLimitV1/V2` and `RepositoryImpl.usageToBalance`) instead of chaining `double` division and multiplication, e.g.:
```java
long unDelegateMaxUsage = BigInteger.valueOf(unDelegateBalance)
    .multiply(BigInteger.valueOf(dynamicStore.getTotalNetLimit()))
    .divide(BigInteger.valueOf(TRX_PRECISION).multiply(BigInteger.valueOf(repo.getTotalNetWeight())))
    .longValueExact();
```
Apply the equivalent fix to both the BANDWIDTH and ENERGY branches in `UnDelegateResourceProcessor` and in `UnDelegateResourceActuator`.

### Proof of Concept
1. Freeze and delegate a bandwidth/energy amount from account A to account B such that `totalNetLimit`/`totalNetWeight` (or `totalEnergyCurrentLimit`/`totalEnergyWeight`) produce a non-terminating fractional ratio (e.g. `totalNetWeight` not evenly dividing `unDelegateBalance * totalNetLimit`).
2. Have account A call `unDelegateResource` (either via the normal `UnDelegateResourceContract` transaction or via a contract invoking the TVM native `unDelegateResource`) to reclaim part of the delegated balance.
3. Compare the resulting `transferUsage`/`NetUsage`/`EnergyUsage` values on accounts A and B against the value computed by multiplying first then dividing with `BigInteger` (as done in `ResourceProcessor.calculateGlobalLimitV1`). The `double`-based division-before-multiplication path in `UnDelegateResourceProcessor.execute`/`UnDelegateResourceActuator` yields a divergent result, demonstrating the precision-loss bug.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/UnDelegateResourceProcessor.java (L115-120)
```java
            // calculate usage
            long unDelegateMaxUsage = (long) ((double) unDelegateBalance / TRX_PRECISION
                * dynamicStore.getTotalNetLimit() / repo.getTotalNetWeight());
            transferUsage = (long) (receiverCapsule.getNetUsage()
                * ((double) (unDelegateBalance) / receiverCapsule.getAllFrozenBalanceForBandwidth()));
            transferUsage = min(unDelegateMaxUsage, transferUsage, VMConfig.disableJavaLangMath());
```

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/UnDelegateResourceProcessor.java (L139-144)
```java
            // calculate usage
            long unDelegateMaxUsage = (long) ((double) unDelegateBalance / TRX_PRECISION
                * dynamicStore.getTotalEnergyCurrentLimit() / repo.getTotalEnergyWeight());
            transferUsage = (long) (receiverCapsule.getEnergyUsage()
                * ((double) (unDelegateBalance) / receiverCapsule.getAllFrozenBalanceForEnergy()));
            transferUsage = min(unDelegateMaxUsage, transferUsage, VMConfig.disableJavaLangMath());
```

**File:** actuator/src/main/java/org/tron/core/actuator/UnDelegateResourceActuator.java (L80-85)
```java
            // calculate usage
            long unDelegateMaxUsage = (long) ((double) unDelegateBalance / TRX_PRECISION
                * ((double) (dynamicStore.getTotalNetLimit()) / dynamicStore.getTotalNetWeight()));
            transferUsage = (long) (receiverCapsule.getNetUsage()
                * ((double) (unDelegateBalance) / receiverCapsule.getAllFrozenBalanceForBandwidth()));
            transferUsage = min(unDelegateMaxUsage, transferUsage);
```

**File:** actuator/src/main/java/org/tron/core/actuator/UnDelegateResourceActuator.java (L103-108)
```java
            // calculate usage
            long unDelegateMaxUsage = (long) ((double) unDelegateBalance / TRX_PRECISION
                * ((double) (dynamicStore.getTotalEnergyCurrentLimit()) / dynamicStore.getTotalEnergyWeight()));
            transferUsage = (long) (receiverCapsule.getEnergyUsage()
                * ((double) (unDelegateBalance) / receiverCapsule.getAllFrozenBalanceForEnergy()));
            transferUsage = min(unDelegateMaxUsage, transferUsage);
```

**File:** chainbase/src/main/java/org/tron/core/db/ResourceProcessor.java (L350-357)
```java
  protected long calculateGlobalLimitV1(long frozeBalance,
      long totalLimit, long totalWeight) {
    long weight = frozeBalance / TRX_PRECISION;
    return BigInteger.valueOf(weight)
        .multiply(BigInteger.valueOf(totalLimit))
        .divide(BigInteger.valueOf(totalWeight))
        .longValueExact();
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/repository/RepositoryImpl.java (L257-266)
```java
  private long usageToBalance(long usage, long totalWeight, long totalLimit) {
    if (hardenResourceCalculation()) {
      return BigInteger.valueOf(usage)
          .multiply(BigInteger.valueOf(totalWeight))
          .multiply(BigInteger.valueOf(TRX_PRECISION))
          .divide(BigInteger.valueOf(totalLimit))
          .longValueExact();
    }
    return (long) ((double) usage * totalWeight / totalLimit * TRX_PRECISION);
  }
```
