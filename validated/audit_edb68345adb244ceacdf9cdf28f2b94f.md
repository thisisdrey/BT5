Based on the investigation, I found a valid analog to the reported bug class ("value validated on one config-mutation path but not on the deploy/creation path").

### Title
CreateSmartContract deployment does not validate `consume_user_resource_percent`, unlike UpdateSettingContract - ([File: framework/src/main/java/org/tron/core/Wallet.java])

### Summary
Similar to the NFTPort finding — where `RuntimeConfig.royaltiesBps` is validated on update but not on deployment — java-tron validates `consume_user_resource_percent` when it is *updated* via `UpdateSettingContractActuator`, but the equivalent validation on the *initial* value supplied at contract-deployment time (`CreateSmartContract`) exists only inside a helper convenience method, `Wallet.createTransactionCapsule`, and is not enforced by an actuator's `validate()` at execution time.

### Finding Description
`UpdateSettingContractActuator.validate()` strictly enforces the invariant `0 <= consume_user_resource_percent <= 100` before allowing an update: [1](#0-0) 

However, the same field on `CreateSmartContract` (deployment time) is *not* checked by any actuator `validate()`. The only place this range check is performed is inline inside the wallet-side helper `Wallet.createTransactionCapsule`, which explicitly skips actuator-level `validate()` for `CreateSmartContract`/`TriggerSmartContract` types and instead does an ad-hoc percent check: [2](#0-1) 

This check lives entirely in a convenience-construction path (`createTransactionCapsule`), not in the actuator validation pipeline that every broadcast transaction goes through (`Actuator.validate()`/`execute()` invoked from `Manager`/`TransactionTrace` during block application and transaction processing). Any transaction built via other code paths — e.g., `createTransactionCapsuleWithoutValidate`, direct gRPC `BroadcastTransaction`, or a manually-crafted signed transaction submitted directly to the node — bypasses this check entirely, since there is no corresponding `validate()` enforcement inside the actuator that actually executes contract creation (the creation-time processing that stores `consumeUserResourcePercent` into the `ContractCapsule`, consumed later in `VMActuator.getTotalEnergyLimitWithFixRatio`): [3](#0-2) 

This mirrors exactly the reported bug class: the field is guarded on the "runtime/update" mutation path (`UpdateSettingContractActuator`) but not on the "deploy" path when the transaction is submitted through any route other than the specific wallet helper method.

### Impact Explanation
`consumeUserResourcePercent` directly controls how energy fee liability is split between the contract's `creator` and the `caller` in `VMActuator.getTotalEnergyLimitWithFixRatio`. A deployed contract with an out-of-range value (e.g., negative, or > 100) stored via `ContractCapsule` (since `CreateSmartContractActuator`/execution path does not clamp/validate it either) can produce miscalculated `creatorEnergyLimit`/`callerEnergyLimit` — including division by an invalid percent or unintended free/unlimited energy for callers — which is an unbacked-resource/incorrect-accounting condition (energy accounting integrity), directly affecting on-chain resource billing and settlement.

### Likelihood Explanation
Any unprivileged deployer who signs and broadcasts a `CreateSmartContract` transaction directly (bypassing the HTTP `Wallet.createTransactionCapsule` helper, e.g., via gRPC `wallet/broadcastTransaction` or by submitting a pre-built raw transaction) can supply an out-of-range `consume_user_resource_percent` value with no on-chain enforcement rejecting it. This requires no special privileges — only the ability to submit a signed transaction, matching the "unprivileged contract deployer" reachability constraint.

### Recommendation
Move the `consume_user_resource_percent` range validation (`0 <= percent <= 100`) into the actual actuator/validate path that processes `CreateSmartContract` at execution/consensus time (not only inside the convenience `Wallet.createTransactionCapsule` helper), so that it is enforced consistently regardless of how the transaction was constructed or submitted, mirroring the existing check in `UpdateSettingContractActuator.validate()`.

### Proof of Concept
1. Construct a `CreateSmartContract` transaction directly (not via `Wallet.createTransactionCapsule`) with `new_contract.consume_user_resource_percent` set to an out-of-range value (e.g., `-1` or `200`).
2. Sign and broadcast the transaction via the gRPC `BroadcastTransaction` API or raw JSON-RPC transaction submission.
3. Observe that the transaction is accepted and the contract is deployed with the invalid percent stored, because no actuator `validate()` step rejects it — contrasted with attempting the same invalid value through `UpdateSettingContractActuator`, which is correctly rejected with `"percent not in [0, 100]"` as shown in [4](#0-3) .

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/UpdateSettingContractActuator.java (L93-97)
```java
    long newPercent = contract.getConsumeUserResourcePercent();
    if (newPercent > ActuatorConstant.ONE_HUNDRED || newPercent < 0) {
      throw new ContractValidateException(
          "percent not in [0, 100]");
    }
```

**File:** framework/src/main/java/org/tron/core/Wallet.java (L479-499)
```java
  public TransactionCapsule createTransactionCapsule(com.google.protobuf.Message message,
      ContractType contractType) throws ContractValidateException {
    TransactionCapsule trx = new TransactionCapsule(message, contractType);
    trx.setTransactionCreate(true);
    if (contractType != ContractType.CreateSmartContract
        && contractType != ContractType.TriggerSmartContract) {
      List<Actuator> actList = ActuatorFactory.createActuator(trx, chainBaseManager);
      for (Actuator act : actList) {
        act.validate();
      }
    }
    trx.setTransactionCreate(false);
    if (contractType == ContractType.CreateSmartContract) {

      CreateSmartContract contract = ContractCapsule
          .getSmartContractFromTransaction(trx.getInstance());
      long percent = contract.getNewContract().getConsumeUserResourcePercent();
      if (percent < 0 || percent > 100) {
        throw new ContractValidateException("percent must be >= 0 and <= 100");
      }
    }
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

**File:** framework/src/test/java/org/tron/core/actuator/UpdateSettingContractActuatorTest.java (L162-181)
```java
  @Test
  public void invalidResourcePercent() {
    UpdateSettingContractActuator actuator =
        new UpdateSettingContractActuator();
    actuator.setChainBaseManager(dbManager.getChainBaseManager())
        .setAny(getContract(OWNER_ADDRESS, CONTRACT_ADDRESS, INVALID_PERCENT));

    TransactionResultCapsule ret = new TransactionResultCapsule();
    try {
      actuator.validate();
      actuator.execute(ret);

      fail("percent not in [0, 100]");
    } catch (ContractValidateException e) {
      Assert.assertTrue(e instanceof ContractValidateException);
      Assert.assertEquals("percent not in [0, 100]", e.getMessage());
    } catch (ContractExeException e) {
      Assert.assertFalse(e instanceof ContractExeException);
    }
  }
```
