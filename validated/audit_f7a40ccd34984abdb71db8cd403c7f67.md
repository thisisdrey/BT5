### Title
Contract owners can instantly change `consumeUserResourcePercent` via `UpdateSettingContract` to front-run callers into paying higher energy fees - (File: `actuator/src/main/java/org/tron/core/actuator/UpdateSettingContractActuator.java`)

### Summary
`UpdateSettingContractActuator` lets a smart contract's owner change `consume_user_resource_percent` for a deployed contract with no timelock, no delay, and no advance notice mechanism. This percentage directly determines how much of a TVM call's energy cost is charged to the calling user versus the contract's creator. Because the change applies immediately at the next block and can be set as high as 100, a contract owner can front-run a pending user transaction by raising the percent right before the user's call executes, silently shifting the entire energy fee burden onto the unsuspecting caller.

### Finding Description
`UpdateSettingContractActuator.execute()` unpacks the caller-supplied `UpdateSettingContract` and immediately writes the new `consume_user_resource_percent` into the target contract's on-chain `ContractCapsule`, with no delay or notification: [1](#0-0) 

`validate()` only checks that the caller is the contract's origin/owner and that the new percent is within `[0, 100]`; there is no cooldown, event-broadcast requirement, or minimum-notice period: [2](#0-1) 

This `consumeUserResourcePercent` value is read directly during TVM execution to split energy consumption between the contract's caller and its origin/creator. In `VMActuator.getTotalEnergyLimitWithFixRatio`, a higher percent reduces the energy allotted to the creator and forces the caller to cover more of the cost: [3](#0-2) 

The same percent is later used in `TransactionTrace.pay()` to compute how the actual energy bill is split when settling the transaction, so the effect on fee liability is fully realized on-chain: [4](#0-3) 

Because a signed `UpdateSettingContract` transaction (or an `UpdateEnergyLimitContract` transaction that similarly changes `origin_energy_limit` at will, see `actuator/src/main/java/org/tron/core/actuator/UpdateEnergyLimitContractActuator.java` lines 40-47) can be broadcast and mined in the block immediately preceding a victim's pending call, the contract owner can raise `consumeUserResourcePercent` to 100 right before the victim's transaction confirms, causing the victim to unexpectedly pay for all energy usage instead of the previously advertised split.

### Impact Explanation
An unprivileged (from the protocol's perspective) but authorized contract owner can weaponize `UpdateSettingContract`/`UpdateEnergyLimitContract` to front-run any user interacting with their contract, forcing that user to pay significantly more TRX in energy fees than expected, with no on-chain warning period. This can be repeated on every block, effectively allowing griefing/economic exploitation of end users who trust a contract's advertised fee-sharing configuration.

### Likelihood Explanation
Any deployed contract's owner already has the privilege to call these actuators; no special permission escalation is required beyond normal ownership of the contract. The only requirement is timing the transaction to land in the block right before the targeted user's transaction, which is a standard front-running technique achievable via mempool observation.

### Recommendation
Introduce a time-delayed activation for `consume_user_resource_percent` and `origin_energy_limit` changes (e.g., queue the change and apply it only after N blocks/a fixed delay), and emit an event/log at the time the change is scheduled so that users interacting with the contract have a window to notice the pending fee-sharing change before it takes effect.

### Proof of Concept
1. Contract owner deploys a contract with `consume_user_resource_percent = 0` (owner covers all energy) via `CreateSmartContract`.
2. A victim observes this setting and submits a `TriggerSmartContract` call expecting to pay no/low energy fee.
3. Before the victim's transaction is included, the owner submits `UpdateSettingContract` setting `consume_user_resource_percent = 100`, processed by `UpdateSettingContractActuator.execute()` at [5](#0-4) .
4. When the victim's call executes in a subsequent block, `VMActuator.getTotalEnergyLimitWithFixRatio` and `TransactionTrace.pay()` now compute the victim as responsible for 100% of the energy cost, causing an unexpected fee charge with no prior notice.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/UpdateSettingContractActuator.java (L40-50)
```java
    try {
      UpdateSettingContract usContract = any.unpack(UpdateSettingContract.class);
      long newPercent = usContract.getConsumeUserResourcePercent();
      byte[] contractAddress = usContract.getContractAddress().toByteArray();
      ContractCapsule deployedContract = contractStore.get(contractAddress);

      contractStore.put(contractAddress, new ContractCapsule(
          deployedContract.getInstance().toBuilder().setConsumeUserResourcePercent(newPercent)
              .build()));
      RepositoryImpl.removeLruCache(contractAddress);

```

**File:** actuator/src/main/java/org/tron/core/actuator/UpdateSettingContractActuator.java (L93-97)
```java
    long newPercent = contract.getConsumeUserResourcePercent();
    if (newPercent > ActuatorConstant.ONE_HUNDRED || newPercent < 0) {
      throw new ContractValidateException(
          "percent not in [0, 100]");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/VMActuator.java (L746-777)
```java
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
```

**File:** chainbase/src/main/java/org/tron/core/db/TransactionTrace.java (L245-256)
```java
        callerAccount = callContract.getOwnerAddress().toByteArray();
        originAccount = contractCapsule.getOriginAddress();
        boolean disableJavaLangMath = dynamicPropertiesStore.disableJavaLangMath();
        percent = max(Constant.ONE_HUNDRED - contractCapsule.getConsumeUserResourcePercent(
            disableJavaLangMath), 0, disableJavaLangMath);
        percent = min(percent, Constant.ONE_HUNDRED,
            disableJavaLangMath);
        originEnergyLimit = contractCapsule.getOriginEnergyLimit();
        break;
      default:
        return;
    }
```
