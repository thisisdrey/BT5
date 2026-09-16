### Title
`RepositoryImpl.calculateGlobalEnergyLimit` divides by zero when `TotalEnergyWeight` is zero, reachable from any TriggerSmartContract/CreateSmartContract transaction - (File: actuator/src/main/java/org/tron/core/vm/repository/RepositoryImpl.java)

### Summary
`RepositoryImpl.calculateGlobalEnergyLimit` computes a global energy limit by dividing by `TotalEnergyWeight` and only guards against a zero denominator with a Java `assert`, which is a no-op in production (assertions are disabled unless the JVM is started with `-ea`). This mirrors the reported Astaria bug class: a denominator used unconditionally in a division whose zero-case is only "checked" by a mechanism that doesn't actually run in production, letting normal user transactions hit a division by zero / undefined arithmetic and revert.

### Finding Description
`RepositoryImpl.calculateGlobalEnergyLimit` performs: [1](#0-0) 

Note the guard is only `assert totalEnergyWeight > 0;` (line 1001) — with no runtime `if` fallback. Compare this to the sibling implementation in `EnergyProcessor.calculateGlobalEnergyLimit`, which was hardened with an actual runtime check: [2](#0-1) 

and `calculateGlobalEnergyLimitV2`, which explicitly returns 0 when the weight is zero: [3](#0-2) 

`RepositoryImpl.calculateGlobalEnergyLimit` has no such `if (totalEnergyWeight <= 0) return 0;` fallback, no `allowNewReward()`-gated short-circuit, and no V1/V2 split — it always executes the division once `frozeBalance >= TRX_PRECISION`. Since Java assertions are disabled by default in a standard production JVM invocation, the `assert totalEnergyWeight > 0` statement never actually executes at runtime, so this code path is functionally identical to the Astaria `mulDivDown` call with an unchecked denominator.

This method is invoked (via `getAccountLeftEnergyFromFreeze`) from the `VMActuator` energy-limit computation paths used on every smart-contract call/deployment: [4](#0-3) [5](#0-4) 

These are called from `create()`/`call()` in `VMActuator`, which back the `TriggerSmartContractActuator`/`CreateSmartContractActuator` execution paths — i.e., any unprivileged account issuing a `TriggerSmartContract` or contract-creation transaction can reach this code.

### Impact Explanation
If `TotalEnergyWeight` (a chain-wide dynamic property tracking legacy freeze-for-energy stake) is ever zero while a caller/creator account still has `getAllFrozenBalanceForEnergy() >= TRX_PRECISION` (e.g., via TVM Freeze V2 balances that don't necessarily keep the legacy weight counter non-zero, or during/after migration windows where legacy freeze weight has fully unwound), the hardened path (`BigInteger.divide`) throws `ArithmeticException: / by zero`, and the non-hardened path performs `totalEnergyLimit / totalEnergyWeight` as a `double`, producing `Infinity`/`NaN` cast to `long`, yielding an undefined/garbage energy limit. Either outcome corrupts energy accounting used to gate and bill smart-contract execution — causing transaction execution failures for legitimate contract calls (denial of service on contract execution) or incorrect energy-limit computation feeding into fee/usage accounting.

### Likelihood Explanation
The exact state (frozen-for-energy balance present while `TotalEnergyWeight == 0`) depends on chain-wide freeze/unfreeze dynamics; I could not fully confirm from the indexed code whether `TotalEnergyWeight` can realistically reach zero once Freeze V2 is fully active without also zeroing individual accounts' `getAllFrozenBalanceForEnergy()`. This is the key uncertainty for exploitability/likelihood, and would need runtime/chain-state verification (e.g., via a Devin session actually tracing `saveTotalEnergyWeight`/`addTotalEnergyWeight` callers across the freeze/unfreeze/delegate actuators) to establish a concrete reachable trigger. Regardless of likelihood of the exact zero-state, the code itself is objectively unsafe: it relies on `assert` for a security-relevant precondition, which is a code-quality/security defect independent of whether the state is currently reachable in practice.

### Recommendation
Align `RepositoryImpl.calculateGlobalEnergyLimit` with the hardened logic already used in `EnergyProcessor`: replace the `assert totalEnergyWeight > 0;` with an actual runtime check that returns 0 (or otherwise safely short-circuits) when `totalEnergyWeight <= 0`, before performing either the `BigInteger` division or the double division, e.g.:
```java
long totalEnergyWeight = getDynamicPropertiesStore().getTotalEnergyWeight();
if (totalEnergyWeight <= 0) {
  return 0;
}
```
Additionally, audit other locations in the codebase relying on bare `assert` statements for values used later as divisors, since asserts are not a substitute for input/state validation in production Java code.

### Proof of Concept
Not independently reproducible from static code review alone — a concrete PoC would require driving `TotalEnergyWeight` to 0 in `DynamicPropertiesStore` while an account retains `getAllFrozenBalanceForEnergy() >= TRX_PRECISION`, then issuing a `TriggerSmartContract` transaction from that account so that `VMActuator.getAccountEnergyLimitWithFixRatio`/`getTotalEnergyLimitWithFixRatio` invoke `RepositoryImpl.calculateGlobalEnergyLimit`. This would need to be validated in a live/test chain environment (e.g., a Devin session running the existing `FreezeV2Test`/`CalculateGlobalLimitHardenTest` harnesses with `TotalEnergyWeight` forced to 0) to confirm the `ArithmeticException` or NaN-cast behavior actually triggers.

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

**File:** actuator/src/main/java/org/tron/core/actuator/VMActuator.java (L583-623)
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

    long energyFromFeeLimit = feeLimit / sunPerEnergy;
    if (VMConfig.allowTvmFreezeV2()) {
      long now = rootRepository.getHeadSlot();
      EnergyProcessor energyProcessor =
          new EnergyProcessor(
              rootRepository.getDynamicPropertiesStore(),
              ChainBaseManager.getInstance().getAccountStore());
      energyProcessor.updateUsage(account);
      account.setLatestConsumeTimeForEnergy(now);
      receipt.setCallerEnergyUsage(account.getEnergyUsage());
      receipt.setCallerEnergyWindowSize(account.getWindowSize(ENERGY));
      receipt.setCallerEnergyWindowSizeV2(account.getWindowSizeV2(ENERGY));
      account.setEnergyUsage(
          energyProcessor.increase(account, ENERGY,
              account.getEnergyUsage(), min(leftFrozenEnergy, energyFromFeeLimit,
                  VMConfig.disableJavaLangMath()), now, now));
      receipt.setCallerEnergyMergedUsage(account.getEnergyUsage());
      receipt.setCallerEnergyMergedWindowSize(account.getWindowSize(ENERGY));
      rootRepository.updateAccount(account.createDbKey(), account);
    }
    return min(availableEnergy, energyFromFeeLimit, VMConfig.disableJavaLangMath());

  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/VMActuator.java (L731-798)
```java
  public long getTotalEnergyLimitWithFixRatio(AccountCapsule creator, AccountCapsule caller,
      TriggerSmartContract contract, long feeLimit, long callValue)
      throws ContractValidateException {

    long callerEnergyLimit = getAccountEnergyLimitWithFixRatio(caller, feeLimit, callValue);
    if (Arrays.equals(creator.getAddress().toByteArray(), caller.getAddress().toByteArray())) {
      // when the creator calls his own contract, this logic will be used.
      // so, the creator must use a BIG feeLimit to call his own contract,
      // which will cost the feeLimit TRX when the creator's frozen energy is 0.
      return callerEnergyLimit;
    }

    long creatorEnergyLimit = 0;
    ContractCapsule contractCapsule = rootRepository
        .getContract(contract.getContractAddress().toByteArray());
    long consumeUserResourcePercent = contractCapsule.getConsumeUserResourcePercent(
        VMConfig.disableJavaLangMath());

    long originEnergyLimit = contractCapsule.getOriginEnergyLimit();
    if (originEnergyLimit < 0) {
      throw new ContractValidateException("originEnergyLimit can't be < 0");
    }

    long originEnergyLeft = 0;
    if (consumeUserResourcePercent < VMConstant.ONE_HUNDRED) {
      originEnergyLeft = rootRepository.getAccountLeftEnergyFromFreeze(creator);
      if (VMConfig.allowTvmFreeze() || VMConfig.allowTvmFreezeV2()) {
        receipt.setOriginEnergyLeft(originEnergyLeft);
      }
    }
    if (consumeUserResourcePercent <= 0) {
      creatorEnergyLimit = min(originEnergyLeft, originEnergyLimit,
          VMConfig.disableJavaLangMath());
    } else {
      if (consumeUserResourcePercent < VMConstant.ONE_HUNDRED) {
        // creatorEnergyLimit =
        // min(callerEnergyLimit * (100 - percent) / percent,
        //   creatorLeftFrozenEnergy, originEnergyLimit)

        creatorEnergyLimit = min(
            BigInteger.valueOf(callerEnergyLimit)
                .multiply(BigInteger.valueOf(VMConstant.ONE_HUNDRED - consumeUserResourcePercent))
                .divide(BigInteger.valueOf(consumeUserResourcePercent)).longValueExact(),
            min(originEnergyLeft, originEnergyLimit, VMConfig.disableJavaLangMath()),
            VMConfig.disableJavaLangMath());
      }
    }
    if (VMConfig.allowTvmFreezeV2()) {
      long now = rootRepository.getHeadSlot();
      EnergyProcessor energyProcessor =
          new EnergyProcessor(
              rootRepository.getDynamicPropertiesStore(),
              ChainBaseManager.getInstance().getAccountStore());
      energyProcessor.updateUsage(creator);
      creator.setLatestConsumeTimeForEnergy(now);
      receipt.setOriginEnergyUsage(creator.getEnergyUsage());
      receipt.setOriginEnergyWindowSize(creator.getWindowSize(ENERGY));
      receipt.setOriginEnergyWindowSizeV2(creator.getWindowSizeV2(ENERGY));
      creator.setEnergyUsage(
          energyProcessor.increase(creator, ENERGY,
              creator.getEnergyUsage(), creatorEnergyLimit, now, now));
      receipt.setOriginEnergyMergedUsage(creator.getEnergyUsage());
      receipt.setOriginEnergyMergedWindowSize(creator.getWindowSize(ENERGY));
      rootRepository.updateAccount(creator.createDbKey(), creator);
    }
    return addExact(callerEnergyLimit, creatorEnergyLimit,
        VMConfig.disableJavaLangMath());
  }
```
