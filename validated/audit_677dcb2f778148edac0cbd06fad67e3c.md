### Title
Contract owner can front-run `TriggerSmartContract` calls by setting `consumeUserResourcePercent` to 100% via `UpdateSettingContract`, shifting the entire energy fee onto the unsuspecting caller - (File: `actuator/src/main/java/org/tron/core/actuator/UpdateSettingContractActuator.java`)

### Summary
`UpdateSettingContractActuator` lets a smart-contract owner change `consumeUserResourcePercent` for a deployed contract at any time, and the new value takes effect immediately for the very next `TriggerSmartContract` transaction that is processed. Because this percentage determines how the energy cost of a call is split between the caller and the contract's own frozen/staked energy, a malicious or compromised contract owner can front-run any pending call to their contract by setting the percent to 100, forcing the unsuspecting caller to pay the contract's entire energy bill out of their own frozen energy/balance instead of the split the caller expected when they crafted their transaction and `feeLimit`.

### Finding Description
`UpdateSettingContractActuator.execute()` immediately persists the new `consumeUserResourcePercent` to the `ContractStore` and invalidates the repository cache with no delay, timelock, or acknowledgement from in-flight callers: [1](#0-0) 

Validation only checks that the value is within `[0, 100]` and that the sender is the contract's origin/owner address — it does not compare against, or require confirmation of, the caller-expected value: [2](#0-1) [3](#0-2) 

That stored percent is then read at execution time inside `VMActuator.getTotalEnergyLimitWithFixRatio` (and the float-ratio equivalent), which computes how much energy the caller vs. the contract creator will fund for a `TriggerSmartContract` call: [4](#0-3) 

When `consumeUserResourcePercent` is 100, the branch that grants the contract's own frozen energy (`creatorEnergyLimit`) is skipped entirely, so the caller alone must fund the whole execution: [5](#0-4) 

An unprivileged transaction broadcaster who calls the contract has no way to know, at the moment they sign and broadcast their `TriggerSmartContract` transaction, that the owner will change this ratio in the same block via `UpdateSettingContract`. This mirrors the `DropEngineV2` pattern in the external report: an owner-controlled parameter that affects fund distribution for a caller's operation is applied instantly and can be front-run against any specific pending transaction.

### Impact Explanation
If the owner front-runs a caller's `TriggerSmartContract` transaction and raises `consumeUserResourcePercent` to 100, the caller is forced to consume their own frozen energy or pay TRX (via `feeLimit`) for energy that they expected the contract deployer to subsidize. This can cause the caller to either overpay TRX unexpectedly (loss of funds relative to their expectation) or have their call fail/burn `feeLimit` due to insufficient allotted energy, which is an unauthorized shifting of costs onto an unprivileged account — a concrete funds-loss impact reachable purely by broadcasting a normal transaction.

### Likelihood Explanation
Requires a malicious or compromised contract owner address, matching the "Low" likelihood classification in the original report, but the owner has unrestricted, unthrottled ability to call `UpdateSettingContract` at will and can trivially observe pending `TriggerSmartContract` transactions targeting their own contract in the mempool to time the front-run.

### Recommendation
Consider adding a caller-supplied "expected consumeUserResourcePercent" field to `TriggerSmartContract` (or otherwise binding the call to the percent value visible when the transaction was signed) so `VMActuator`/`call()` can revert if the on-chain value differs from what the caller expected, analogous to the report's slippage-style recommendation. Alternatively, introduce a delay (e.g., apply-at-next-block or timelock) between an `UpdateSettingContract` transaction and when the new percent becomes effective for calls, preventing same-block front-running.

### Proof of Concept
1. Attacker deploys/owns a contract `C` with `consumeUserResourcePercent = 0` (fully subsidized calls).
2. Victim observes this and broadcasts a `TriggerSmartContract` transaction to call `C`, sizing `feeLimit` on the assumption that `C`'s own frozen energy covers most of the cost.
3. Attacker (owner of `C`) broadcasts `UpdateSettingContract` with `consumeUserResourcePercent = 100` and has it included in the same block ahead of the victim's transaction (validated by `UpdateSettingContractActuator.validate/execute`).
4. When the victim's transaction executes, `VMActuator.getTotalEnergyLimitWithFixRatio` sees `consumeUserResourcePercent = 100`, so `creatorEnergyLimit = 0` and the victim's own energy/`feeLimit` funds the entire call, resulting in unexpected TRX loss or transaction failure with fee consumption for the victim.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/UpdateSettingContractActuator.java (L41-49)
```java
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

**File:** actuator/src/main/java/org/tron/core/actuator/UpdateSettingContractActuator.java (L107-113)
```java
    byte[] deployedContractOwnerAddress = deployedContract.getInstance().getOriginAddress()
        .toByteArray();

    if (!Arrays.equals(ownerAddress, deployedContractOwnerAddress)) {
      throw new ContractValidateException(
          ACCOUNT_EXCEPTION_STR + readableOwnerAddress + "] is not the owner of the contract");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/VMActuator.java (L744-777)
```java
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
```
