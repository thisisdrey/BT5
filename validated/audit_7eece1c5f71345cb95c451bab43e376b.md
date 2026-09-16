Based on my investigation, I found a concrete analog to the reported bug class ("actuator/validate path enforces a limit that a separate execution path for the same operation omits").

### Title
Missing `supportUnfreezeDelay` feature-gate check in `DelegateResourceProcessor.validate()` allows TVM contracts to bypass committee-controlled delegate-resource gating enforced by `DelegateResourceActuator` - (File: `actuator/src/main/java/org/tron/core/vm/nativecontract/DelegateResourceProcessor.java`)

### Summary
Just as the external report describes `LendingPool.borrowFor()` reaching a supply-affecting code path without re-checking a cap that the "normal" entry point enforces, java-tron has two independent entry points for the same `DelegateResourceContract` operation: the transaction-level `DelegateResourceActuator` (reached by a signed `DelegateResourceContract` transaction) and the TVM-level `DelegateResourceProcessor` (reached when a smart contract calls the `delegateResource` opcode helper in `Program.java`). Both mutate the same account/resource state, but only the actuator enforces the `supportUnfreezeDelay` committee gate and the delegate lock-period rules; the processor's `validate()` omits them.

### Finding Description
`DelegateResourceActuator.validate()` requires the chain to have `supportUnfreezeDelay()` enabled before permitting any delegate-resource transaction: [1](#0-0) 

It also enforces committee-controlled lock-period limits (`getMaxDelegateLockPeriod()`), and the remaining-lock-time rule via `validRemainTime`: [2](#0-1) 

The TVM-reachable `DelegateResourceProcessor.validate()`, invoked from the `delegateResource` precompiled/native-contract opcode helper in `Program.java`, only checks `supportDR()`, address validity, account existence, minimum balance, resource-availability math, and receiver checks. It never checks `supportUnfreezeDelay()`, and it has no lock/lock-period logic at all (the `DelegateResourceParam` used by this path carries no lock fields): [3](#0-2) 

The TVM entry point that reaches this validator from arbitrary contract bytecode is: [4](#0-3) 

This mirrors the reported bug class exactly: a state-mutating operation is exposed through two code paths, and a governance/limit check that protects the "intended" path is missing from the alternate path that ultimately performs the same underlying state mutation (`delegateResource(...)`, `addDelegatedFrozenV2Balance...`, `addFrozenBalanceForBandwidthV2/EnergyV2`).

### Impact Explanation
`supportUnfreezeDelay` is the committee-controlled feature switch that gates the entire FreezeV2/DelegateResource V2 model. If the SR committee has not yet enabled this feature (or disables it again), delegate-resource operations issued as ordinary transactions are correctly rejected. However, any deployed smart contract can still invoke the `delegateResource` native opcode and mutate `FrozenV2Balance`, `DelegatedFrozenV2Balance`, and `AcquiredDelegatedFrozenV2Balance` fields on-chain regardless of this feature flag, and regardless of committee-configured lock-period limits. This creates a state divergence between what governance intends to permit and what is actually reachable, and can produce delegated-resource state that downstream unfreeze/undelegate logic does not expect (since much of that logic assumes the V2 feature and its lock-period constraints were enforced at entry). This is a governance-bypass / inconsistent-state class of impact reachable by any account able to deploy and trigger a contract.

### Likelihood Explanation
The path is reachable by any unprivileged account: deploy a contract that calls the `delegateResource` TVM helper (already exercised by existing tests such as `PrecompiledContractsTest.resourceV2Test` and `FreezeV2Test.testDelegateResourceOperations`), and trigger it via an ordinary trigger-smart-contract transaction. No special privileges are required, and the resource-balance checks in the processor do not compensate for the missing `supportUnfreezeDelay` and lock-period checks.

### Recommendation
Add the same `dynamicStore.supportUnfreezeDelay()` gate to `DelegateResourceProcessor.validate()` that exists in `DelegateResourceActuator.validate()`, and audit whether lock-period semantics available in the transaction-level contract should also be enforced (or explicitly and safely disabled) for the TVM-triggered path, so that both entry points to the same underlying state mutation enforce identical governance and limit checks.

### Proof of Concept
1. On a network where the committee has not enabled `supportUnfreezeDelay` (or has disabled it), submitting a `DelegateResourceContract` transaction directly fails validation with `"Not support Delegate resource transaction, need to be opened by the committee"` per `DelegateResourceActuator.validate()`.
2. Deploy a contract exercising the `delegateResource(address,uint256,uint256)` TVM opcode (as in `FreezeV2Test.testDelegateResourceOperations` / `PrecompiledContractsTest.resourceV2Test`), after first freezing balance via `freezeBalanceV2`.
3. Trigger the contract's delegate call; `DelegateResourceProcessor.validate()` does not check `supportUnfreezeDelay()`, so the call succeeds and mutates `FrozenV2Balance`/`DelegatedFrozenV2Balance`/`AcquiredDelegatedFrozenV2Balance` even though the equivalent plain transaction would have been rejected.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/DelegateResourceActuator.java (L118-125)
```java
    if (!dynamicStore.supportDR()) {
      throw new ContractValidateException("No support for resource delegate");
    }

    if (!dynamicStore.supportUnfreezeDelay()) {
      throw new ContractValidateException("Not support Delegate resource transaction,"
          + " need to be opened by the committee");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/DelegateResourceActuator.java (L211-241)
```java
    boolean lock = delegateResourceContract.getLock();
    if (lock && dynamicStore.supportMaxDelegateLockPeriod()) {
      long lockPeriod = getLockPeriod(true, delegateResourceContract);
      long maxDelegateLockPeriod = dynamicStore.getMaxDelegateLockPeriod();
      if (lockPeriod < 0 || lockPeriod > maxDelegateLockPeriod) {
        throw new ContractValidateException(
            "The lock period of delegate resource cannot be less than 0 and cannot exceed "
                + maxDelegateLockPeriod + "!");
      }

      byte[] key = DelegatedResourceCapsule.createDbKeyV2(ownerAddress, receiverAddress, true);
      DelegatedResourceCapsule delegatedResourceCapsule = delegatedResourceStore.get(key);
      long now = dynamicStore.getLatestBlockHeaderTimestamp();
      if (delegatedResourceCapsule != null) {
        switch (delegateResourceContract.getResource()) {
          case BANDWIDTH: {
            validRemainTime(BANDWIDTH, lockPeriod,
                delegatedResourceCapsule.getExpireTimeForBandwidth(), now);
          }
          break;
          case ENERGY: {
            validRemainTime(ENERGY, lockPeriod,
                delegatedResourceCapsule.getExpireTimeForEnergy(), now);
          }
          break;
          default:
            throw new ContractValidateException(
                "ResourceCode error, valid ResourceCode[BANDWIDTH、ENERGY]");
        }
      }
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/DelegateResourceProcessor.java (L33-115)
```java
  public void validate(DelegateResourceParam param, Repository repo) throws ContractValidateException {
    if (repo == null) {
      throw new ContractValidateException(STORE_NOT_EXIST);
    }

    byte[] ownerAddress = param.getOwnerAddress();
    DynamicPropertiesStore dynamicStore = repo.getDynamicPropertiesStore();
    if (!dynamicStore.supportDR()) {
      throw new ContractValidateException("No support for resource delegate");
    }
    if (!DecodeUtil.addressValid(ownerAddress)) {
      throw new ContractValidateException("Invalid address");
    }
    AccountCapsule ownerCapsule = repo.getAccount(ownerAddress);
    if (ownerCapsule == null) {
      String readableOwnerAddress = StringUtil.createReadableString(ownerAddress);
      throw new ContractValidateException(
          ActuatorConstant.ACCOUNT_EXCEPTION_STR + readableOwnerAddress + NOT_EXIST_STR);
    }
    long delegateBalance = param.getDelegateBalance();
    if (delegateBalance < TRX_PRECISION) {
      throw new ContractValidateException("delegateBalance must be greater than or equal to 1 TRX");
    }

    boolean disableJavaLangMath = VMConfig.disableJavaLangMath();
    switch (param.getResourceType()) {
      case BANDWIDTH: {
        BandwidthProcessor processor = new BandwidthProcessor(ChainBaseManager.getInstance());
        processor.updateUsageForDelegated(ownerCapsule);

        long netUsage = (long) (ownerCapsule.getNetUsage() * TRX_PRECISION * ((double)
            (repo.getTotalNetWeight()) / dynamicStore.getTotalNetLimit()));

        long v2NetUsage = getV2NetUsage(ownerCapsule, netUsage, disableJavaLangMath);

        if (ownerCapsule.getFrozenV2BalanceForBandwidth() - v2NetUsage < delegateBalance) {
          throw new ContractValidateException(
                  "delegateBalance must be less than or equal to available FreezeBandwidthV2 balance");
        }
      }
      break;
      case ENERGY: {
        EnergyProcessor processor =
            new EnergyProcessor(dynamicStore, ChainBaseManager.getInstance().getAccountStore());
        processor.updateUsage(ownerCapsule);

        long energyUsage = (long) (ownerCapsule.getEnergyUsage() * TRX_PRECISION * ((double)
            (repo.getTotalEnergyWeight()) / dynamicStore.getTotalEnergyCurrentLimit()));

        long v2EnergyUsage = getV2EnergyUsage(ownerCapsule, energyUsage, disableJavaLangMath);

        if (ownerCapsule.getFrozenV2BalanceForEnergy() - v2EnergyUsage < delegateBalance) {
          throw new ContractValidateException(
                  "delegateBalance must be less than or equal to available FreezeEnergyV2 balance");
        }
      }
      break;
      default:
        throw new ContractValidateException(
            "Unknown ResourceCode, valid ResourceCode[BANDWIDTH、ENERGY]");
    }

    byte[] receiverAddress = param.getReceiverAddress();

    if (!DecodeUtil.addressValid(receiverAddress)) {
      throw new ContractValidateException("Invalid receiverAddress");
    }
    if (Arrays.equals(receiverAddress, ownerAddress)) {
      throw new ContractValidateException(
          "receiverAddress must not be the same as ownerAddress");
    }
    AccountCapsule receiverCapsule = repo.getAccount(receiverAddress);
    if (receiverCapsule == null) {
      String readableOwnerAddress = StringUtil.createReadableString(receiverAddress);
      throw new ContractValidateException(
          ActuatorConstant.ACCOUNT_EXCEPTION_STR
              + readableOwnerAddress + NOT_EXIST_STR);
    }
    if (receiverCapsule.getType() == Protocol.AccountType.Contract) {
      throw new ContractValidateException(
          "Do not allow delegate resources to contract addresses");
    }
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L2168-2200)
```java
  public boolean delegateResource(
      DataWord receiverAddress, DataWord delegateBalance, DataWord resourceType) {
    Repository repository = getContractState().newRepositoryChild();
    byte[] owner = getContextAddress();
    byte[] receiver = receiverAddress.toTronAddress();

    increaseNonce();
    InternalTransaction internalTx = addInternalTx(null, owner, receiver,
        delegateBalance.longValue(), null,
        "delegateResourceOf" + convertResourceToString(resourceType), nonce, null);

    try {
      DelegateResourceParam param = new DelegateResourceParam();
      param.setOwnerAddress(owner);
      param.setReceiverAddress(receiver);
      param.setDelegateBalance(delegateBalance.sValue().longValueExact());
      param.setResourceType(parseResourceCodeV2(resourceType));

      DelegateResourceProcessor processor = new DelegateResourceProcessor();
      processor.validate(param, repository);
      processor.execute(param, repository);
      repository.commit();
      return true;
    } catch (ContractValidateException e) {
      logger.warn("TVM DelegateResource: validate failure. Reason: {}", e.getMessage());
    } catch (ArithmeticException e) {
      logger.warn("TVM DelegateResource: balance out of long range.");
    }
    if (internalTx != null) {
      internalTx.reject();
    }
    return false;
  }
```
