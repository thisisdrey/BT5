### Title
`UpdateSettingContract` lets a contract owner front-run pending calls by spiking `consume_user_resource_percent` with no timelock - (File: actuator/src/main/java/org/tron/core/actuator/UpdateSettingContractActuator.java)

### Summary
`UpdateSettingContractActuator` lets the owner of a deployed smart contract change `consume_user_resource_percent` — the ratio that decides how much of a triggered call's energy cost is billed to the *caller* versus the contract owner — to any value between 0 and 100, with immediate effect and no cooldown/timelock. This mirrors the Yieldy `setFee()` bug: an admin-controlled parameter that determines how much of a transaction's value another user pays can be changed atomically right before that user's transaction executes, letting the privileged party unexpectedly shift cost onto an unsuspecting caller.

### Finding Description
`UpdateSettingContractActuator.execute()` unconditionally applies the new percentage supplied by the contract owner directly to `ContractCapsule`, and the change is visible to the very next transaction that triggers the contract: [1](#0-0) 

The only guard in `validate()` is a coarse bound check (0–100) and an ownership check — there is no upper limit tighter than the theoretical maximum, no minimum-change delay, and no mechanism for a caller to specify/enforce the percentage they agreed to when they built and signed their `TriggerSmartContract`: [2](#0-1) 

The updated percentage takes effect immediately for the next transaction in the same or a subsequent block — the actuator test explicitly demonstrates this immediate-effect behavior across two sequential actuator executions: [3](#0-2) 

This percentage directly controls how the energy cost of a `TriggerSmartContract` call is split between the calling account and the contract's creator account, as exercised in `getTotalEnergyLimitWithFixRatio`: [4](#0-3) 

This is structurally identical to the reported class of bug: a single-transaction, admin-controlled parameter (`fee` in Yieldy vs. `consume_user_resource_percent` in java-tron) that determines the economic split of a subsequent user transaction, changeable up to its maximum value with no upper cap tighter than the trivial bound and no timelock — enabling a contract owner to front-run a pending `TriggerSmartContract` transaction from any caller and shift most or all of the energy cost onto that caller without their agreement.

### Impact Explanation
A malicious (or careless) contract owner can observe a pending `TriggerSmartContract` call to their contract in the mempool and broadcast an `UpdateSettingContract` transaction that raises `consume_user_resource_percent` to 100 just before it executes. The caller — who budgeted `fee_limit`/bandwidth expecting the previous (e.g., low) percentage — is then charged energy fees at the new, unexpected rate. This can (a) unexpectedly drain a caller's TRX/energy resources far beyond what they intended to spend on a call, or (b) cause their transaction to fail with `OUT_OF_ENERGY` after consuming resources, both of which are direct, unauthorized economic harm to an unprivileged caller triggered purely by a permissionless, single-signed transaction from the contract owner.

### Likelihood Explanation
Exploitation requires only that the attacker be the owner of a deployed contract (no special/validator/witness privilege) and be able to observe a pending call to their own contract and get their `UpdateSettingContract` transaction included before it — a standard mempool-visibility frontrunning scenario, not reliant on colluding with a block-producing SR. This is a routine capability for any contract owner, making the likelihood non-trivial for any contract that is not fully trusted by its callers.

### Recommendation
- Introduce an upper bound significantly below 100% (or a maximum step-size per update) for `consume_user_resource_percent` changes, and/or
- Add a timelock/delay (e.g., only take effect after the next maintenance cycle, analogous to how `UpdateBrokerageContract` changes are deferred to the next cycle via `DelegationStore`/`MaintenanceManager.doMaintenance()`) so that callers cannot be front-run within the same block, and/or
- Allow callers to specify an expected/maximum acceptable `consume_user_resource_percent` in `TriggerSmartContract` (similar to the `expected` slippage-protection field in `ExchangeTransactionContract`) so the actuator can reject execution if the live value differs from what the caller agreed to.

### Proof of Concept
1. Contract owner deploys a contract with `consume_user_resource_percent = 10` via `SmartContract`/`CreateSmartContract`.
2. User A observes the low percentage, builds and signs a `TriggerSmartContract` transaction with `fee_limit` sized for a 10% cost split, and broadcasts it.
3. The contract owner sees User A's pending transaction, and broadcasts an `UpdateSettingContract` transaction setting `consume_user_resource_percent = 100` for the same contract, getting it included in the same or an earlier-ordered position within the block (`UpdateSettingContractActuator.execute()`/`ContractStore.put()` — [1](#0-0) ).
4. User A's `TriggerSmartContract` then executes against the new 100% split (`getTotalEnergyLimitWithFixRatio`), consuming far more of User A's resources/TRX than expected, or failing with `OUT_OF_ENERGY` after resources are already spent.

Note: I was unable to fully trace `VMActuator.java`'s exact energy-billing logic within the available iterations (only test usage of `getTotalEnergyLimitWithFixRatio` was confirmed), so the precise numeric billing formula in `VMActuator` should be verified directly in that file before remediation.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/UpdateSettingContractActuator.java (L40-51)
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

      ret.setStatus(fee, code.SUCESS);
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

**File:** framework/src/test/java/org/tron/core/actuator/UpdateSettingContractActuatorTest.java (L227-266)
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

    } catch (ContractValidateException e) {
      Assert.assertFalse(e instanceof ContractValidateException);
    } catch (ContractExeException e) {
      Assert.assertFalse(e instanceof ContractExeException);
    }
  }
```

**File:** framework/src/test/java/org/tron/common/runtime/RuntimeImplTest.java (L270-298)
```java
  @Test
  public void getCallerAndCreatorEnergyLimit2With40PercentTest()
      throws ContractExeException, ReceiptCheckErrException, VMIllegalException,
      ContractValidateException {

    long value = 0;
    long feeLimit = 1_000_000_000L; // sun
    long consumeUserResourcePercent = 40L;
    long creatorEnergyLimit = 5_000L;
    String contractName = "test";
    String ABI = "[{\"constant\":true,\"inputs\":[{\"name\":\"count\",\"type\":\"uint256\"}],\""
        + "name\":\"testConstant\",\"outputs\":[],\"payable\":false,\"stateMutability\":\"view\""
        + ",\"type\":\"function\"},{\"constant\":false,\"inputs\":[{\"name\":\"count\",\"type\":"
        + "\"uint256\"}],\"name\":\"testNotConstant\",\"outputs\":[],\"payable\":false,\""
        + "stateMutability\":\"nonpayable\",\"type\":\"function\"}]";
    String code = "608060405234801561001057600080fd5b50610112806100206000396000f300608060405260043"
        + "6106049576000357c0100000000000000000000000000000000000000000000000000000000900463ffffff"
        + "ff16806321964a3914604e5780634c6bb6eb146078575b600080fd5b348015605957600080fd5b50607660"
        + "04803603810190808035906020019092919050505060a2565b005b348015608357600080fd5b5060a060048"
        + "03603810190808035906020019092919050505060c4565b005b600080600091505b8282101560bf57600190"
        + "5060018201915060aa565b505050565b600080600091505b8282101560e1576001905060018201915060cc5"
        + "65b5050505600a165627a7a72305820267cf0ebf31051a92ff62bed7490045b8063be9f1e1a22d07dce2576"
        + "54c8c17b0029";
    TVMTestResult result = TvmTestUtils
        .deployContractWithCreatorEnergyLimitAndReturnTvmTestResult(contractName, creatorAddress,
            ABI, code, value,
            feeLimit, consumeUserResourcePercent, null, dbManager, null,
            creatorEnergyLimit);

```
