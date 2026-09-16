## Title
Frontrunning of `consume_user_resource_percent` via `UpdateSettingContract` allows contract owners to unexpectedly shift energy fees onto callers - (File: actuator/src/main/java/org/tron/core/actuator/UpdateSettingContractActuator.java)

### Summary
The `UpdateSettingContract` actuator lets a smart-contract's deployer (`origin_address`) instantly change `consume_user_resource_percent` for their deployed contract, with no timelock, cooldown, or delayed activation. This value determines how the energy cost of a `TriggerSmartContract` call is split between the caller and the contract owner. Because the change takes effect on the very next block/transaction, a contract owner can advertise a caller-friendly percentage (e.g. 0%, meaning the owner pays all energy), wait for a user to build and broadcast a call expecting that split, then frontrun the user's pending transaction with an `UpdateSettingContract` transaction that raises the percentage to 100% before the user's call is packed — silently shifting the entire energy cost of the call onto the unsuspecting caller.

### Finding Description
`UpdateSettingContractActuator.execute` unconditionally overwrites `consume_user_resource_percent` on the target `ContractCapsule` as soon as the transaction is packed, with validation only checking that the value is within `[0,100]` and that the sender is the contract's `origin_address`: [1](#0-0) [2](#0-1) 

There is no rate limiting or timelock — a contract owner can call this repeatedly and it takes effect immediately, as demonstrated by the actuator test that successfully changes the percent twice in a row with no restriction: [3](#0-2) 

This percentage is read live at execution time in `VMActuator.getTotalEnergyLimitWithFixRatio`/`getTotalEnergyLimitWithFloatRatio`, which computes how much energy the caller vs. the contract creator pays for a `TriggerSmartContract` call based on the *current* `consumeUserResourcePercent` stored on the contract: [4](#0-3) 

Because `consume_user_resource_percent` is read at the moment the caller's transaction is actually executed (not at the time it was signed/broadcast), a contract owner who observes an unconfirmed `TriggerSmartContract` transaction in the mempool can broadcast (and get packed ahead of it, e.g. by paying more bandwidth/priority or via witness collusion) an `UpdateSettingContract` transaction that raises the percent from a caller-friendly value (e.g. 0, "the contract absorbs all energy cost") to 100 ("caller absorbs all energy cost") immediately before the user's call executes. The user's transaction, which was built and signed assuming the previously observed 0% split, now consumes the caller's own frozen/staked energy or burns TRX for energy at the caller's expense.

### Impact Explanation
Energy fees on TRON directly translate into TRX cost (paid via frozen energy or burned from the caller's balance when energy is insufficient, subject to `fee_limit`). If a contract advertises `consume_user_resource_percent = 0` to attract users (the contract pays all energy) and the owner frontruns to `100` right before a high-energy call is executed, the caller unexpectedly pays the entire energy bill for that call, which can be substantial for gas-heavy contract interactions. This is an unauthorized shift of funds/fees from what the caller reasonably expected based on on-chain state, directly analogous to the Buffer `minFee` frontrunning issue where the owner instantly raises a fee parameter right before it is charged to an unsuspecting user.

### Likelihood Explanation
Any account that is the `origin_address` of a deployed smart contract (an ordinary, unprivileged contract deployer — no SR/witness/committee role required) can perform this attack unilaterally by broadcasting a single `UpdateSettingContract` transaction. No approval process, delay, or governance step exists. The attack requires only that the owner's `UpdateSettingContract` transaction be packed before the victim's `TriggerSmartContract` transaction within the same or an earlier block, which is achievable through normal transaction ordering/priority or by contract owners who are also block producers.

### Recommendation
Introduce a timelock or delayed-activation mechanism for `consume_user_resource_percent` changes (e.g., queue the new value and only apply it after N blocks), or snapshot the percentage used for fee-splitting at the time a `TriggerSmartContract` transaction is signed/simulated (e.g., via `fee_limit`/expected-cost validation) so that in-flight calls are not affected by an owner's last-moment adjustment.

### Proof of Concept
1. Contract owner deploys a contract with `consume_user_resource_percent = 0` (owner pays all energy) via `CreateSmartContract`.
2. Victim observes this setting, builds and signs a `TriggerSmartContract` call expecting to pay no energy, and broadcasts it.
3. Owner observes the pending transaction and broadcasts an `UpdateSettingContract` transaction (see `getContract` builder in the test) setting `consume_user_resource_percent = 100`, ensuring it is packed in the same or an earlier block, e.g. via `UpdateSettingContractActuator.execute` as exercised in: [5](#0-4) 
4. When the victim's `TriggerSmartContract` transaction executes, `VMActuator.getTotalEnergyLimitWithFixRatio` reads the now-100% setting and assigns the entire energy cost to the caller.
5. The victim pays energy/TRX fees they did not anticipate when they signed and broadcast the transaction.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/UpdateSettingContractActuator.java (L40-49)
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

**File:** framework/src/test/java/org/tron/core/actuator/UpdateSettingContractActuatorTest.java (L95-118)
```java
  @Test
  public void successUpdateSettingContract() {
    UpdateSettingContractActuator actuator =
        new UpdateSettingContractActuator();
    actuator.setChainBaseManager(dbManager.getChainBaseManager())
        .setAny(getContract(OWNER_ADDRESS, CONTRACT_ADDRESS, TARGET_PERCENT));

    TransactionResultCapsule ret = new TransactionResultCapsule();
    try {
      actuator.validate();
      actuator.execute(ret);

      // assert result state and consume_user_resource_percent
      Assert.assertEquals(ret.getInstance().getRet(), Protocol.Transaction.Result.code.SUCESS);
      Assert.assertEquals(
          dbManager.getContractStore().get(ByteArray.fromHexString(CONTRACT_ADDRESS))
              .getConsumeUserResourcePercent(
                  dbManager.getDynamicPropertiesStore().disableJavaLangMath()), TARGET_PERCENT);
    } catch (ContractValidateException e) {
      Assert.assertFalse(e instanceof ContractValidateException);
    } catch (ContractExeException e) {
      Assert.assertFalse(e instanceof ContractExeException);
    }
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
