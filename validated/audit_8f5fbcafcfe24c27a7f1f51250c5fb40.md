### Title
`TransferAssetActuator.validate()` is missing the version-1 (EVM-compatible) smart-contract transfer restriction present in the sibling `TransferActuator.validate()` - (File: actuator/src/main/java/org/tron/core/actuator/TransferActuator.java, actuator/src/main/java/org/tron/core/actuator/TransferAssetActuator.java)

### Summary
`TransferContract` (plain TRX transfer) and `TransferAssetContract` (TRC10 asset transfer) are sibling actuators that both implement the same "cannot send value to a smart contract" pattern gated by `forbidTransferToContract`. However, `TransferActuator.validate()` contains an additional, second guard that is completely absent from `TransferAssetActuator.validate()`: when `AllowTvmCompatibleEvm` is enabled, sending value to a version-1 (EVM-compatible) contract via a plain system-actuator transfer is explicitly rejected, and the caller is told to use `TriggerSmartContract` instead. The TRC10 transfer path has no equivalent check.

### Finding Description
In `TransferActuator.validate()`: [1](#0-0) 
the code blocks TRX transfers both to any contract (`forbidTransferToContract`) and, additionally, to a contract whose `ContractCapsule.getContractVersion() == 1` when `AllowTvmCompatibleEvm` is active, with the message directing the user to `TriggerSmartContract` instead.

`TransferAssetActuator.validate()` implements only the first check: [2](#0-1) 
It checks `forbidTransferToContract` but has no `AllowTvmCompatibleEvm`/`getContractVersion() == 1` branch at all. The same asymmetry propagates into `TransferAssetActuator.execute()`, which unconditionally credits the TRC10 asset to `toAccountCapsule` via `addAssetAmountV2`: [3](#0-2) 

Version-1 ("EVM-compatible") contracts receive special-cased VM behavior throughout the TVM execution engine (e.g. `gasPriceAction`, `getCallEnergy`, `getCreateEnergy` all branch on `getContractVersion() == 1` under `VMConfig.allowTvmCompatibleEvm()`): [4](#0-3) [5](#0-4) 
This indicates that this contract class is deliberately treated as a distinct execution/compatibility mode that the developers decided must not receive value transfers outside of `TriggerSmartContract` — a rule TransferActuator enforces but TransferAssetActuator does not.

### Impact Explanation
Because `TransferAssetActuator` omits the version-1 restriction, an unprivileged transaction sender can use a plain `TransferAssetContract` (TRC10 transfer) to move token balances directly into a version-1/EVM-compatible contract's account, bypassing the restriction that the developers imposed specifically for `TransferContract`. This is an inconsistency in access/behavior control between two contract types performing conceptually identical value-transfer operations, allowing TRC10 balances to reach a contract state that the protocol otherwise deliberately shields from non-`TriggerSmartContract` value delivery. Depending on how the EVM-compatible contract's TRC10 accounting assumptions work (e.g., contracts relying on `TriggerSmartContract`-only delivery for TRC10 token accounting, similar to Solidity `receive()`/fallback assumptions), this could allow assets to become logically stuck or unaccounted-for within the contract (permanent freezing of funds) or invalidate invariants that depend on all transfers to version-1 contracts flowing exclusively through the VM path.

### Likelihood Explanation
Likelihood is High for reachability (this is a directly broadcastable `TransferAssetContract` transaction requiring no special privilege — any TRC10 holder can trigger it), but the on-chain condition (`AllowTvmCompatibleEvm` enabled and a target being a version-1 contract) must be active, which is a fork/committee-gated feature. I was not able to fully verify the intended consequence of receiving un-triggered TRC10 value at a version-1 contract (i.e., what specifically breaks) since I do not have deeper access to how version-1 contract TRC10 accounting differs; this is a gap in my investigation.

### Recommendation
Add the same `AllowTvmCompatibleEvm`/`getContractVersion() == 1` guard used in `TransferActuator.validate()` (lines 141–156) into `TransferAssetActuator.validate()`'s existing `toAccount != null` branch (lines 169–175), so that TRC10 transfers to version-1 contracts are rejected consistently with plain TRX transfers, directing callers to `TriggerSmartContract`.

### Proof of Concept
1. Enable `AllowTvmCompatibleEvm` via committee proposal (`ALLOW_TVM_COMPATIBLE_EVM`, id 60).
2. Deploy a contract via `CreateSmartContract`; with `AllowTvmCompatibleEvm` on, `VMActuator.create()` sets `newSmartContract` version to `1`: [6](#0-5) 
3. Broadcast a `TransferAssetContract` (TRC10 transfer) with `toAddress` set to this version-1 contract's address, `assetName`/`amount` set to a TRC10 token owned by the sender.
4. `TransferAssetActuator.validate()` passes (only `forbidTransferToContract` is checked, not the version-1 guard), and `execute()` credits the TRC10 balance into the contract's account via `addAssetAmountV2`, bypassing the restriction enforced for the equivalent `TransferContract` (TRX) case.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/TransferActuator.java (L132-156)
```java
      //after ForbidTransferToContract proposal, send trx to smartContract by actuator is not allowed.
      if (dynamicStore.getForbidTransferToContract() == 1
          && toAccount != null
          && toAccount.getType() == AccountType.Contract) {

        throw new ContractValidateException("Cannot transfer TRX to a smartContract.");

      }

      // after AllowTvmCompatibleEvm proposal, send trx to smartContract which version is one
      // by actuator is not allowed.
      if (dynamicStore.getAllowTvmCompatibleEvm() == 1
          && toAccount != null
          && toAccount.getType() == AccountType.Contract) {

        ContractCapsule contractCapsule = chainBaseManager.getContractStore().get(toAddress);
        if (contractCapsule == null) { //  this can not happen
          throw new ContractValidateException(
              "Account type is Contract, but it is not exist in contract store.");
        } else if (contractCapsule.getContractVersion() == 1) {
          throw new ContractValidateException(
              "Cannot transfer TRX to a smartContract which version is one. "
                  + "Instead please use TriggerSmartContract ");
        }
      }
```

**File:** actuator/src/main/java/org/tron/core/actuator/TransferAssetActuator.java (L60-84)
```java
      byte[] ownerAddress = transferAssetContract.getOwnerAddress().toByteArray();
      byte[] toAddress = transferAssetContract.getToAddress().toByteArray();
      AccountCapsule toAccountCapsule = accountStore.get(toAddress);
      if (toAccountCapsule == null) {
        boolean withDefaultPermission =
            dynamicStore.getAllowMultiSign() == 1;
        toAccountCapsule = new AccountCapsule(ByteString.copyFrom(toAddress), AccountType.Normal,
            dynamicStore.getLatestBlockHeaderTimestamp(), withDefaultPermission, dynamicStore);
        accountStore.put(toAddress, toAccountCapsule);

        fee = fee + dynamicStore.getCreateNewAccountFeeInSystemContract();
      }
      ByteString assetName = transferAssetContract.getAssetName();
      long amount = transferAssetContract.getAmount();

      AccountCapsule ownerAccountCapsule = accountStore.get(ownerAddress);
      if (!ownerAccountCapsule
          .reduceAssetAmountV2(assetName.toByteArray(), amount, dynamicStore, assetIssueStore)) {
        throw new ContractExeException("reduceAssetAmount failed !");
      }
      accountStore.put(ownerAddress, ownerAccountCapsule);

      toAccountCapsule
          .addAssetAmountV2(assetName.toByteArray(), amount, dynamicStore, assetIssueStore);
      accountStore.put(toAddress, toAccountCapsule);
```

**File:** actuator/src/main/java/org/tron/core/actuator/TransferAssetActuator.java (L169-192)
```java
    AccountCapsule toAccount = accountStore.get(toAddress);
    if (toAccount != null) {
      //after ForbidTransferToContract proposal, send trx to smartContract by actuator is not allowed.
      if (dynamicStore.getForbidTransferToContract() == 1
          && toAccount.getType() == AccountType.Contract) {
        throw new ContractValidateException("Cannot transfer asset to smartContract.");
      }

      assetBalance = toAccount.getAsset(dynamicStore, ByteArray.toStr(assetName));
      if (assetBalance != null) {
        try {
          assetBalance = addExact(assetBalance, amount); //check if overflow
        } catch (Exception e) {
          logger.debug(e.getMessage(), e);
          throw new ContractValidateException(e.getMessage());
        }
      }
    } else {
      fee = fee + dynamicStore.getCreateNewAccountFeeInSystemContract();
      if (ownerAccount.getBalance() < fee) {
        throw new ContractValidateException(
            "Validate TransferAssetActuator error, insufficient fee.");
      }
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/OperationActions.java (L443-450)
```java
  public static void gasPriceAction(Program program) {
    DataWord energyPrice = DataWord.ZERO();
    if (VMConfig.allowTvmCompatibleEvm() && program.getContractVersion() == 1) {
      energyPrice = new DataWord(program.getContractState()
          .getDynamicPropertiesStore().getEnergyFee());
    }
    program.stackPush(energyPrice);
    program.step();
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L1853-1869)
```java
  public DataWord getCallEnergy(DataWord requestedEnergy, DataWord availableEnergy) {
    if (VMConfig.allowTvmCompatibleEvm() && getContractVersion() == 1) {
      DataWord availableEnergyReduce = availableEnergy.clone();
      availableEnergyReduce.div(new DataWord(64));
      availableEnergy.sub(availableEnergyReduce);
    }
    return requestedEnergy.compareTo(availableEnergy) > 0 ? availableEnergy : requestedEnergy;
  }

  public DataWord getCreateEnergy(DataWord availableEnergy) {
    if (VMConfig.allowTvmCompatibleEvm() && getContractVersion() == 1) {
      DataWord availableEnergyReduce = availableEnergy.clone();
      availableEnergyReduce.div(new DataWord(64));
      availableEnergy.sub(availableEnergyReduce);
    }
    return availableEnergy;
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/VMActuator.java (L340-345)
```java
    SmartContract newSmartContract;
    if (VMConfig.allowTvmCompatibleEvm()) {
      newSmartContract = contract.getNewContract().toBuilder().setVersion(1).build();
    } else {
      newSmartContract = contract.getNewContract().toBuilder().clearVersion().build();
    }
```
