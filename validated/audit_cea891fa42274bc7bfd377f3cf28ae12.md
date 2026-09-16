### Title
Smart contract owner can front-run `TriggerSmartContract` calls by repeatedly changing `consume_user_resource_percent` to unfairly shift energy cost onto callers - (File: actuator/src/main/java/org/tron/core/actuator/UpdateSettingContractActuator.java)

### Summary
`UpdateSettingContractActuator` lets a deployed contract's origin owner change `consume_user_resource_percent` at any time, with no cooldown, no bounds beyond `[0,100]`, and no history/commitment mechanism, exactly like the Napier `poolOwner::setFeeParameter` bug class. Since this percentage directly determines how the energy cost of every subsequent `TriggerSmartContract` call is split between the caller and the contract owner, an owner can advertise a low percentage to attract callers, then front-run an incoming call to raise it to 100 (forcing the caller to pay the full energy cost from their own resources/TRX), and reset it back afterward to lure the next victim — repeating this indefinitely.

### Finding Description
`UpdateSettingContractActuator.execute` unconditionally overwrites the contract's `consume_user_resource_percent` field with the value supplied in the transaction, with no delay or rate limit: [1](#0-0) 

`validate()` only checks that the caller is the contract's `origin_address` and that the new percent is within `[0, 100]`, allowing it to be updated as often as desired, including value `100` (maximum): [2](#0-1) 

This is confirmed by the actuator test explicitly exercising back-to-back updates of the same contract within immediate succession (`twiceUpdateSettingContract`): [3](#0-2) 

The value is read live (not snapshotted) at the moment any `TriggerSmartContract` transaction executes, inside `VMActuator.getTotalEnergyLimitWithFixRatio`, which computes how much energy the caller vs. the contract owner will pay for the call: [4](#0-3) 

When `consumeUserResourcePercent` is 100, `creatorEnergyLimit` stays `0` and the caller's own frozen energy / TRX (via `feeLimit`) must cover the entire call, whereas at a low percentage the contract owner's frozen energy absorbs most of the cost. Because this parameter can be changed in the same block right before a victim's `TriggerSmartContract` transaction is processed, a malicious contract owner can:
1. Set `consume_user_resource_percent` low to advertise cheap calls and attract users.
2. Front-run an incoming call and raise it to `100`.
3. Let the victim's transaction execute, forcing the victim to burn its own TRX/energy for the entire call cost.
4. Reset the percent back to a low value to lure the next victim, and repeat.

### Impact Explanation
Users calling the contract have no way to know, at broadcast time, what percentage will actually be in effect when their transaction is included, so they can be forced to consume far more of their own energy/TRX than they were led to expect for identical calls — a repeatable, owner-controlled resource-cost manipulation directly analogous to the Napier pool-fee front-running issue, resulting in unexpected loss of funds/resources for unprivileged callers.

### Likelihood Explanation
Any account that deployed (or is `origin_address` of) a smart contract can perform this at will, with a single `UpdateSettingContract` transaction before and after each victim call — no special privilege (SR/witness/committee) is required, and the mechanism can be repeated indefinitely at negligible cost since `calcFee()` for `UpdateSettingContractActuator` is `0`: [5](#0-4) 

### Recommendation
Introduce a timelock/cooldown or a deferred-activation mechanism (e.g., activate at the next block/epoch rather than immediately) for `consume_user_resource_percent` updates so that in-flight or freshly broadcast `TriggerSmartContract` transactions cannot be front-run with a maliciously raised percentage.

### Proof of Concept
1. Contract owner deploys a contract and sets `consume_user_resource_percent` = 10 (via `UpdateSettingContract`), publicly implying users pay only 10% of energy costs.
2. A user submits `TriggerSmartContract` expecting cheap execution based on the advertised 10%.
3. The owner observes the pending transaction and submits `UpdateSettingContract` with `consume_user_resource_percent` = 100, which is processed first (front-run) using `UpdateSettingContractActuator.execute`.
4. The victim's `TriggerSmartContract` is then processed by `VMActuator.getTotalEnergyLimitWithFixRatio`, where `consumeUserResourcePercent` is now 100, so `creatorEnergyLimit` = 0 and the victim's own frozen energy/TRX pays the full cost.
5. The owner submits another `UpdateSettingContract` resetting the percentage back to 10 to attract the next victim, and repeats the cycle.

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

**File:** actuator/src/main/java/org/tron/core/actuator/UpdateSettingContractActuator.java (L93-113)
```java
    long newPercent = contract.getConsumeUserResourcePercent();
    if (newPercent > ActuatorConstant.ONE_HUNDRED || newPercent < 0) {
      throw new ContractValidateException(
          "percent not in [0, 100]");
    }

    byte[] contractAddress = contract.getContractAddress().toByteArray();
    ContractCapsule deployedContract = contractStore.get(contractAddress);

    if (deployedContract == null) {
      throw new ContractValidateException(
          "Contract does not exist");
    }

    byte[] deployedContractOwnerAddress = deployedContract.getInstance().getOriginAddress()
        .toByteArray();

    if (!Arrays.equals(ownerAddress, deployedContractOwnerAddress)) {
      throw new ContractValidateException(
          ACCOUNT_EXCEPTION_STR + readableOwnerAddress + "] is not the owner of the contract");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/UpdateSettingContractActuator.java (L123-126)
```java
  @Override
  public long calcFee() {
    return 0;
  }
```

**File:** framework/src/test/java/org/tron/core/actuator/UpdateSettingContractActuatorTest.java (L227-259)
```java
  @Test
  public void twiceUpdateSettingContract() {
    UpdateSettingContractActuator actuator =
        new UpdateSettingContractActuator();
    actuator.setChainBaseManager(dbManager.getChainBaseManager())
        .setAny(getContract(OWNER_ADDRESS, CONTRACT_ADDRESS, TARGET_PERCENT));

    UpdateSettingContractActuator secondActuator =
        new UpdateSettingContractActuator();
    secondActuator.setChainBaseManager(dbManager.getChainBaseManager())
        .setAny(getContract(OWNER_ADDRESS, CONTRACT_ADDRESS, 90L));

    TransactionResultCapsule ret = new TransactionResultCapsule();
    try {
      // first
      actuator.validate();
      actuator.execute(ret);

      Assert.assertEquals(ret.getInstance().getRet(), Protocol.Transaction.Result.code.SUCESS);
      Assert.assertEquals(
          dbManager.getContractStore().get(ByteArray.fromHexString(CONTRACT_ADDRESS))
              .getConsumeUserResourcePercent(
                  dbManager.getDynamicPropertiesStore().disableJavaLangMath()), TARGET_PERCENT);

      // second
      secondActuator.validate();
      secondActuator.execute(ret);

      Assert.assertEquals(ret.getInstance().getRet(), Protocol.Transaction.Result.code.SUCESS);
      Assert.assertEquals(
          dbManager.getContractStore().get(ByteArray.fromHexString(CONTRACT_ADDRESS))
              .getConsumeUserResourcePercent(
                  dbManager.getDynamicPropertiesStore().disableJavaLangMath()), 90L);
```

**File:** actuator/src/main/java/org/tron/core/actuator/VMActuator.java (L743-777)
```java
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
```
