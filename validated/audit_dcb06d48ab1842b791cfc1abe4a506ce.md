## Finding [1](#0-0) [2](#0-1) 

### Title
Contract owner can instantly change `consumeUserResourcePercent` / `originEnergyLimit` with no timelock, silently shifting energy costs onto unaware callers - (File: actuator/src/main/java/org/tron/core/actuator/UpdateSettingContractActuator.java)

### Summary
`UpdateSettingContractActuator` (backing the `UpdateSettingContract` transaction type) lets a smart-contract owner change `consumeUserResourcePercent` for a deployed contract in a single signed transaction, and `UpdateEnergyLimitContractActuator` lets the owner change `originEnergyLimit` the same way [3](#0-2) [4](#0-3) . Both changes take effect immediately on the very next block/transaction with no delay, notice period, or announcement mechanism — exactly the class of issue flagged in the external `setPlatformFee()` report (a critical, user-impacting parameter can be changed instantly with no timelock).

### Finding Description
`consumeUserResourcePercent` determines what fraction of a contract call's energy/bandwidth cost is paid by the contract's own frozen resources versus the calling account's resources; `originEnergyLimit` caps how much energy the contract owner is willing to subsidize per transaction. Both are read live by the VM/receipt-charging logic on every contract invocation. The owner can change either value via a single `UpdateSettingContractActuator`/`UpdateEnergyLimitContractActuator` transaction that is applied synchronously in `execute()`, immediately overwriting the stored `ContractCapsule` and invalidating any cached repository entry [5](#0-4) . There is no cooldown, no minimum-notice window, and no historical/cycle-delayed application analogous to how e.g. brokerage-ratio changes are only applied at the next voting cycle in `MaintenanceManager.doMaintenance()` [6](#0-5) . Unprivileged accounts calling the contract have no way to know, in advance, that the percentage of resource cost billed to them is about to jump (e.g. from 0% to 100%).

### Impact Explanation
An unprivileged transaction broadcaster who interacts with a contract can, without warning, be charged energy/bandwidth costs it did not budget for the moment the contract owner submits an `UpdateSettingContract`/`UpdateEnergyLimitContract` transaction, because the change is live for the very next transaction in the same or next block. This can cause otherwise-valid calls to unexpectedly fail (out-of-energy) or to consume far more of the caller's own frozen/staked resources or TRX (for energy purchased on the market) than anticipated — an unbacked, unannounced shift of cost onto ordinary users, matching the "critical change with no time to react" impact called out in the original report.

### Likelihood Explanation
The transaction requires only the contract owner (deployer) address as `owner_address`, and validation is minimal — address validity, account existence, ownership match, and bound checks (`0-100` for percent, `>0` for energy limit) [7](#0-6) [8](#0-7) . Nothing prevents repeated, rapid toggling; the test suite itself demonstrates the value can be changed twice in immediate succession [9](#0-8) . Any dApp/contract owner can trigger this at will, so likelihood of occurrence (intentionally or carelessly) is high; it does not require any privileged network role (SR/witness/committee).

### Recommendation
Introduce a timelock/delay for `UpdateSettingContract` and `UpdateEnergyLimitContract` changes analogous to the cycle-based delay already used for brokerage updates: stage the new `consumeUserResourcePercent`/`originEnergyLimit` value and only apply it after a fixed number of blocks/maintenance cycles, and/or emit an on-chain event so integrators/callers can react before the change takes effect.

### Proof of Concept
1. Deploy a contract `C` with `consumeUserResourcePercent = 0` (fully subsidized by the contract owner).
2. Users begin calling `C`, paying no energy themselves and budgeting transactions accordingly.
3. Owner submits `UpdateSettingContract{contract_address=C, consume_user_resource_percent=100}`; `UpdateSettingContractActuator.execute()` applies it immediately [10](#0-9) .
4. The very next call to `C` by any unprivileged user is billed 100% of the energy cost with no prior warning, potentially failing or draining the caller's frozen energy/bandwidth unexpectedly.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/UpdateSettingContractActuator.java (L31-58)
```java
  @Override
  public boolean execute(Object object) throws ContractExeException {
    TransactionResultCapsule ret = (TransactionResultCapsule) object;
    if (Objects.isNull(ret)) {
      throw new RuntimeException(ActuatorConstant.TX_RESULT_NULL);
    }

    long fee = calcFee();
    ContractStore contractStore = chainBaseManager.getContractStore();
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
    } catch (InvalidProtocolBufferException e) {
      logger.debug(e.getMessage(), e);
      ret.setStatus(fee, code.FAILED);
      throw new ContractExeException(e.getMessage());
    }
    return true;
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/UpdateSettingContractActuator.java (L93-97)
```java
    long newPercent = contract.getConsumeUserResourcePercent();
    if (newPercent > ActuatorConstant.ONE_HUNDRED || newPercent < 0) {
      throw new ContractValidateException(
          "percent not in [0, 100]");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/UpdateEnergyLimitContractActuator.java (L30-57)
```java
  @Override
  public boolean execute(Object object) throws ContractExeException {
    TransactionResultCapsule ret = (TransactionResultCapsule) object;
    if (Objects.isNull(ret)) {
      throw new RuntimeException(ActuatorConstant.TX_RESULT_NULL);
    }

    long fee = calcFee();
    ContractStore contractStore = chainBaseManager.getContractStore();
    try {
      UpdateEnergyLimitContract usContract = any.unpack(UpdateEnergyLimitContract.class);
      long newOriginEnergyLimit = usContract.getOriginEnergyLimit();
      byte[] contractAddress = usContract.getContractAddress().toByteArray();
      ContractCapsule deployedContract = contractStore.get(contractAddress);

      contractStore.put(contractAddress, new ContractCapsule(
          deployedContract.getInstance().toBuilder().setOriginEnergyLimit(newOriginEnergyLimit)
              .build()));
      RepositoryImpl.removeLruCache(contractAddress);

      ret.setStatus(fee, code.SUCESS);
    } catch (InvalidProtocolBufferException e) {
      logger.debug(e.getMessage(), e);
      ret.setStatus(fee, code.FAILED);
      throw new ContractExeException(e.getMessage());
    }
    return true;
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/UpdateEnergyLimitContractActuator.java (L97-101)
```java
    long newOriginEnergyLimit = contract.getOriginEnergyLimit();
    if (newOriginEnergyLimit <= 0) {
      throw new ContractValidateException(
          "origin energy limit must be > 0");
    }
```

**File:** consensus/src/main/java/org/tron/consensus/dpos/MaintenanceManager.java (L154-162)
```java
    if (dynamicPropertiesStore.allowChangeDelegation()) {
      long nextCycle = dynamicPropertiesStore.getCurrentCycleNumber() + 1;
      dynamicPropertiesStore.saveCurrentCycleNumber(nextCycle);
      consensusDelegate.getAllWitnesses().forEach(witness -> {
        delegationStore.setBrokerage(nextCycle, witness.createDbKey(),
            delegationStore.getBrokerage(witness.createDbKey()));
        delegationStore.setWitnessVote(nextCycle, witness.createDbKey(), witness.getVoteCount());
      });
    }
```

**File:** framework/src/test/java/org/tron/core/actuator/UpdateEnergyLimitContractActuatorTest.java (L229-263)
```java
  @Test
  public void twiceUpdateEnergyLimitContract() throws InvalidProtocolBufferException {
    UpdateEnergyLimitContractActuator actuator = new UpdateEnergyLimitContractActuator();
    actuator.setChainBaseManager(dbManager.getChainBaseManager())
        .setAny(getContract(OWNER_ADDRESS, CONTRACT_ADDRESS, TARGET_ENERGY_LIMIT));

    UpdateEnergyLimitContractActuator secondActuator = new UpdateEnergyLimitContractActuator();
    secondActuator.setChainBaseManager(dbManager.getChainBaseManager())
        .setAny(getContract(OWNER_ADDRESS, CONTRACT_ADDRESS, 90L));

    TransactionResultCapsule ret = new TransactionResultCapsule();
    try {
      // first
      actuator.validate();
      actuator.execute(ret);

      Assert.assertEquals(OWNER_ADDRESS,
          ByteArray.toHexString(actuator.getOwnerAddress().toByteArray()));
      Assert.assertEquals(ret.getInstance().getRet(), Protocol.Transaction.Result.code.SUCESS);
      Assert.assertEquals(
          dbManager.getContractStore().get(ByteArray.fromHexString(CONTRACT_ADDRESS))
              .getOriginEnergyLimit(), TARGET_ENERGY_LIMIT);

      // second
      secondActuator.validate();
      secondActuator.execute(ret);

      Assert.assertEquals(ret.getInstance().getRet(), Protocol.Transaction.Result.code.SUCESS);
      Assert.assertEquals(
          dbManager.getContractStore().get(ByteArray.fromHexString(CONTRACT_ADDRESS))
              .getOriginEnergyLimit(), 90L);
    } catch (ContractValidateException | ContractExeException e) {
      Assert.fail(e.getMessage());
    }
  }
```
