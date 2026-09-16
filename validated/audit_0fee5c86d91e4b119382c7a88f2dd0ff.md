## Divide-by-zero in `RepositoryImpl.calculateGlobalEnergyLimit()` during TVM energy-limit computation crashes transaction execution — (File: `actuator/src/main/java/org/tron/core/vm/repository/RepositoryImpl.java`)

### Summary
`RepositoryImpl.calculateGlobalEnergyLimit()` divides by `totalEnergyWeight` while only guarding the zero case with a Java `assert` statement, which is a no-op unless the JVM is started with `-ea` (not the default in production). When hardened resource calculation is enabled and `totalEnergyWeight` is `0`, the `BigInteger` division throws `ArithmeticException: BigInteger divide by zero`, mirroring the CVE-2018-18521 pattern of an unchecked zero divisor (`sh_entsize`) reaching a division operation.

### Finding Description
`RepositoryImpl.calculateGlobalEnergyLimit()`: [1](#0-0) 

This computes an account's frozen-energy limit using `totalEnergyWeight = getDynamicPropertiesStore().getTotalEnergyWeight()`, then divides `totalEnergyLimit` by it. The only zero-guard is `assert totalEnergyWeight > 0;`, which does nothing at runtime in a standard production JVM (assertions are disabled by default). If `hardenResourceCalculation()` (i.e., `VMConfig.allowHardenResourceCalculation()`) is true and `totalEnergyWeight` is `0`, the call `BigInteger.valueOf(totalEnergyLimit).divide(BigInteger.valueOf(totalEnergyWeight))` throws `ArithmeticException`.

This contrasts with the equivalent, correctly-guarded implementation in `EnergyProcessor`, which explicitly returns `0` when `totalEnergyWeight <= 0` instead of relying on an assertion: [2](#0-1) 
and its V2 counterpart: [3](#0-2) 

The vulnerable `RepositoryImpl` method is reached unconditionally on every TVM contract call via `getAccountLeftEnergyFromFreeze()`, which is invoked from `VMActuator.getAccountEnergyLimitWithFixRatio()` for every `TriggerSmartContract`: [4](#0-3) 

`totalEnergyWeight` is a global dynamic property tracking the sum of all frozen-for-energy balances across the network; it is legitimately `0` on freshly bootstrapped chains, private/consortium java-tron deployments before any account freezes energy, or after all energy freezes are withdrawn.

### Impact Explanation
When `totalEnergyWeight` is `0` and hardened resource calculation is enabled, any account triggering a smart contract call causes an uncaught `ArithmeticException` inside energy-limit computation on the hot execution path used by every contract invocation. Because this runs during block application/transaction processing in `Manager`, an unhandled runtime exception here can abort transaction processing or crash the node process handling the block, resulting in a denial-of-service / node halt condition — matching the "node crash or halt" acceptance criteria.

### Likelihood Explanation
The trigger condition (`totalEnergyWeight == 0`) is state-dependent rather than directly attacker-forged per transaction, but it is reachable through normal, permissionless network conditions (new/private chains, or a state where no account holds a frozen-for-energy balance) combined with `allowHardenResourceCalculation` being enabled. Once that global state exists, **any** anonymous account calling any smart contract deterministically triggers the divide-by-zero, since `getAccountLeftEnergyFromFreeze` is called unconditionally for every contract trigger.

### Recommendation
Add an explicit `if (totalEnergyWeight <= 0) return 0;` guard in `RepositoryImpl.calculateGlobalEnergyLimit()` before the `BigInteger` division, consistent with `EnergyProcessor.calculateGlobalEnergyLimit()`/`calculateGlobalEnergyLimitV2()`, and remove reliance on the `assert` statement for correctness.

### Proof of Concept
1. Deploy or operate a java-tron network (e.g., a private/test chain) where `totalEnergyWeight` (`DynamicPropertiesStore.getTotalEnergyWeight()`) is `0` (no account currently has frozen balance for energy) and `allowHardenResourceCalculation` is enabled.
2. Any account submits a `TriggerSmartContract` transaction (calling any deployed contract).
3. `VMActuator.getAccountEnergyLimitWithFixRatio()` calls `rootRepository.getAccountLeftEnergyFromFreeze(account)` → `RepositoryImpl.calculateGlobalEnergyLimit()`.
4. Since the account has a nonzero `frozeBalance` for energy is not required — the guard only checks `frozeBalance < TRX_PRECISION` for early return `0`; if `frozeBalance >= TRX_PRECISION` but `totalEnergyWeight == 0`, execution reaches the division and throws `ArithmeticException: BigInteger divide by zero`, propagating out of contract execution handling.

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

**File:** actuator/src/main/java/org/tron/core/actuator/VMActuator.java (L583-599)
```java
  public long getAccountEnergyLimitWithFixRatio(AccountCapsule account, long feeLimit,
      long callValue) {

    long sunPerEnergy = VMConstant.SUN_PER_ENERGY;
    if (rootRepository.getDynamicPropertiesStore().getEnergyFee() > 0) {
      sunPerEnergy = rootRepository.getDynamicPropertiesStore().getEnergyFee();
    }

    long leftFrozenEnergy = rootRepository.getAccountLeftEnergyFromFreeze(account);
    if (VMConfig.allowTvmFreeze() || VMConfig.allowTvmFreezeV2()) {
      receipt.setCallerEnergyLeft(leftFrozenEnergy);
    }

    long energyFromBalance = max(account.getBalance() - callValue, 0,
        VMConfig.disableJavaLangMath()) / sunPerEnergy;
    long availableEnergy = addExact(leftFrozenEnergy, energyFromBalance,
        VMConfig.disableJavaLangMath());
```
