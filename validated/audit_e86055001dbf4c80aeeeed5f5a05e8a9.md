### Title
Division by zero in `RepositoryImpl.calculateGlobalEnergyLimit` when `totalEnergyWeight` is zero, reachable from TVM `freezeBalanceV2`/delegation opcodes - (File: actuator/src/main/java/org/tron/core/vm/repository/RepositoryImpl.java)

### Summary
`RepositoryImpl.calculateGlobalEnergyLimit(AccountCapsule)` divides `energyWeight * totalEnergyLimit` by `totalEnergyWeight` without any explicit runtime guard against `totalEnergyWeight == 0`. The only protection is a Java `assert` statement, which is compiled out and disabled by default (Java assertions are off unless the JVM is started with `-ea`), so in a normal production deployment the check is a no-op.

### Finding Description
The method reads:
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
``` [1](#0-0) 

This mirrors the exact bug class described in the CVE report: an ALU-style division/modulo operation where the divisor (here `totalEnergyWeight`, the analog of the BPF `#k` immediate) is not checked before use, and the only nominal safeguard (`assert`) does not execute at runtime in a standard deployment.

Contrast with the sibling implementations in `EnergyProcessor.calculateGlobalEnergyLimit`/`calculateGlobalEnergyLimitV2` and `BandwidthProcessor.calculateGlobalNetLimit`/`calculateGlobalNetLimitV2`, which explicitly guard: `if (totalEnergyWeight == 0) { return 0; }` before doing any division [2](#0-1) , [3](#0-2) . `RepositoryImpl.calculateGlobalEnergyLimit` (the TVM-reachable copy used inside the `Repository`/`ContractState` abstraction) lacks this guard, relying solely on the disabled `assert`.

The method is exposed through the `Repository` interface and delegated by `ContractState.calculateGlobalEnergyLimit` [4](#0-3) , which is the object used inside TVM execution (`Program`/native contract processors invoked by contract calls such as `freezeBalanceV2`, `unfreezeBalanceV2`, `delegateResource`/`unDelegateResource` opcodes, all reachable by an ordinary transaction sender through `VMActuator`).

### Impact Explanation
When `hardenResourceCalculation()` is enabled (`allowHardenResourceCalculation`), a zero `totalEnergyWeight` causes `BigInteger.divide` to throw `ArithmeticException: BigInteger divide by zero`. If this exception is not caught along the specific call path that reaches this helper (unlike callers such as `WithdrawRewardProcessor`/`VoteWitnessProcessor` that explicitly catch `ArithmeticException`), it propagates as an uncaught runtime exception during transaction/contract execution, which can abort block application or crash the node process executing the TVM opcode — a denial-of-service against a live full node. In the non-hardened path, double division by zero silently returns `Infinity`/`NaN`, which truncates to an incorrect (potentially huge or zero) energy limit value used for freeze/energy accounting, opening a path to corrupted or unbacked energy-limit computations rather than an immediate crash.

### Likelihood Explanation
`totalEnergyWeight` becomes zero only in edge conditions (e.g., no accounts have frozen TRX for energy on the network, or right after a network reset / chain state where all energy stake has been fully unfrozen). This is a narrow, low-frequency condition rather than something an attacker can trivially force on demand on a live mainnet with existing stakers, which lowers likelihood versus the libpcap bug (which is trivially triggered by any crafted filter program). Still, on lower-activity/private/test networks, or transiently during specific state transitions, the precondition is reachable without any privileged access, purely through freeze/energy-delegation transactions.

### Recommendation
Add an explicit runtime guard (not relying on `assert`) in `RepositoryImpl.calculateGlobalEnergyLimit`, mirroring `EnergyProcessor`/`BandwidthProcessor`: return `0` (or another safe default) when `totalEnergyWeight <= 0`, before doing either the `BigInteger` division or the double division. This removes the disabled-by-default `assert` as the sole protection.

### Proof of Concept
Not independently reproducible from the index alone with full confidence — the exact scenario under which `totalEnergyWeight` reaches `0` while `RepositoryImpl.calculateGlobalEnergyLimit` (as opposed to the guarded `EnergyProcessor` version) is invoked from a live TVM opcode path was not fully traced end-to-end due to tool/iteration limits; a background Devin session with full-repository access would be needed to confirm the concrete call chain (`Program` → native contract processor → `Repository.calculateGlobalEnergyLimit`) and construct an exact transaction sequence that drives `totalEnergyWeight` to zero and triggers the uncaught `ArithmeticException`.

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

**File:** chainbase/src/main/java/org/tron/core/db/EnergyProcessor.java (L145-179)
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

**File:** chainbase/src/main/java/org/tron/core/db/BandwidthProcessor.java (L432-466)
```java
  public long calculateGlobalNetLimit(AccountCapsule accountCapsule) {
    long frozeBalance = accountCapsule.getAllFrozenBalanceForBandwidth();
    if (dynamicPropertiesStore.supportUnfreezeDelay()) {
      return calculateGlobalNetLimitV2(frozeBalance);
    }
    if (frozeBalance < TRX_PRECISION) {
      return 0;
    }
    long totalNetLimit = chainBaseManager.getDynamicPropertiesStore().getTotalNetLimit();
    long totalNetWeight = chainBaseManager.getDynamicPropertiesStore().getTotalNetWeight();
    if (dynamicPropertiesStore.allowNewReward() && totalNetWeight <= 0) {
      return 0;
    }
    if (totalNetWeight == 0) {
      return 0;
    }
    if (hardenCalculation()) {
      return calculateGlobalLimitV1(frozeBalance, totalNetLimit, totalNetWeight);
    }
    long netWeight = frozeBalance / TRX_PRECISION;
    return (long) (netWeight * ((double) totalNetLimit / totalNetWeight));
  }

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

**File:** actuator/src/main/java/org/tron/core/vm/program/ContractState.java (L287-290)
```java
  @Override
  public long calculateGlobalEnergyLimit(AccountCapsule accountCapsule) {
    return repository.calculateGlobalEnergyLimit(accountCapsule);
  }
```
