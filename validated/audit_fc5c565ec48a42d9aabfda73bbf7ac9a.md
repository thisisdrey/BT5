### Title
Missing "no delegate to contract address" check in `DelegateResourceActuator` allows resource delegation to smart contracts, unlike the TVM-reachable native path - ([File: actuator/src/main/java/org/tron/core/actuator/DelegateResourceActuator.java])

### Summary
The bug class in the report is a "missed override" pattern: a state-changing operation is exposed through two different entry points, and an access/sanity check that is supposed to gate that operation is implemented on only one of the entry points. In java-tron, resource delegation (`DelegateResourceContract`) is reachable both as a directly broadcast transaction (`DelegateResourceActuator`) and as a TVM native contract invoked from inside a smart contract (`DelegateResourceProcessor`). The native-contract path enforces a check that the receiver of delegated resources must not be a contract address, but the transaction actuator path that any account can broadcast directly does not enforce this check.

### Finding Description
`DelegateResourceProcessor.validate()` (used when resource delegation is triggered from within the TVM, i.e. via a smart-contract call) explicitly rejects delegation to a contract receiver: [1](#0-0) 

`DelegateResourceActuator.validate()`, which is the code path executed for a plain, directly-signed `DelegateResourceContract` transaction (broadcastable by any account), performs the analogous owner/balance/lock validations but never checks the receiver's account type: [2](#0-1) 

Both entry points call into equivalent state-mutating logic (`ownerCapsule.addDelegatedFrozenV2BalanceForBandwidth/Energy`, `DelegatedResourceCapsule` bookkeeping), so they are meant to enforce the same invariants: [3](#0-2) [4](#0-3) 

The related `UnDelegateResourceActuator`/`UnDelegateResourceProcessor` code contains a comment acknowledging the special-case risk of a delegatee being a contract that later self-destructs, leaving delegated resource bookkeeping in an inconsistent state: [5](#0-4) 

Because `DelegateResourceActuator` omits the contract-receiver check that `DelegateResourceProcessor` enforces, any unprivileged transaction broadcaster can delegate BANDWIDTH/ENERGY directly to a contract address using a plain signed `DelegateResourceContract` transaction, bypassing the restriction the protocol clearly intends to enforce uniformly (as evidenced by the native-contract-path check and the suicide-handling comment in the unDelegate code).

### Impact Explanation
Delegating resources to a contract address that can later `SUICIDE`/self-destruct and be re-created creates the exact inconsistent-state scenario the codebase's own comments call out ("A TVM contract suicide, re-create will produce this situation"), where `receiverCapsule.getAcquiredDelegatedFrozenV2BalanceForBandwidth/Energy` no longer matches the true expectations of the delegation bookkeeping. This can leave delegated TRX resource balances in a state where reconciliation on unDelegate is unreliable, a resource-accounting integrity issue consistent with the "unbacked balance" / inconsistent-state impact class. It does not directly enable third-party theft of another account's assets (the owner still controls when to unDelegate), which limits the severity compared to the ERC4626 report's direct asset theft, but it is a genuine and reachable access-control asymmetry between two entry points implementing the same operation.

### Likelihood Explanation
The path is trivially reachable: anyone with a signed account (owner of a `DelegateResourceContract`) can broadcast this contract type without going through the TVM, so the missing check requires no special privileges — only building a `DelegateResourceContract` naming a contract address as `receiver_address`.

### Recommendation
Add the same "receiver must not be a contract address" check found in `DelegateResourceProcessor.validate()` to `DelegateResourceActuator.validate()`, so both delegation entry points enforce identical invariants.

### Proof of Concept
1. Deploy any smart contract on TRON, obtaining its contract address `C`.
2. As an unprivileged account `A` that has frozen V2 balance for BANDWIDTH or ENERGY, construct and broadcast a `DelegateResourceContract` transaction with `owner_address = A`, `receiver_address = C`, and a valid `balance`.
3. Observe that `DelegateResourceActuator.validate()` (actuator/src/main/java/org/tron/core/actuator/DelegateResourceActuator.java) does not reject `C` for being a contract account, and the delegation succeeds — whereas the same delegation attempted via the TVM native path (`DelegateResourceProcessor.validate()`) would be rejected with "Do not allow delegate resources to contract addresses".

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/DelegateResourceProcessor.java (L104-114)
```java
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
```

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/DelegateResourceProcessor.java (L117-144)
```java
  public void execute(DelegateResourceParam param, Repository repo) {
    byte[] ownerAddress = param.getOwnerAddress();
    AccountCapsule ownerCapsule = repo.getAccount(param.getOwnerAddress());
    long delegateBalance = param.getDelegateBalance();
    byte[] receiverAddress = param.getReceiverAddress();

    // delegate resource to receiver
    switch (param.getResourceType()) {
      case BANDWIDTH:
        delegateResource(ownerAddress, receiverAddress, true,
            delegateBalance, repo);

        ownerCapsule.addDelegatedFrozenV2BalanceForBandwidth(delegateBalance);
        ownerCapsule.addFrozenBalanceForBandwidthV2(-delegateBalance);
        break;
      case ENERGY:
        delegateResource(ownerAddress, receiverAddress, false,
            delegateBalance, repo);

        ownerCapsule.addDelegatedFrozenV2BalanceForEnergy(delegateBalance);
        ownerCapsule.addFrozenBalanceForEnergyV2(-delegateBalance);
        break;
      default:
        logger.debug("Resource Code Error.");
    }

    repo.updateAccount(ownerCapsule.createDbKey(), ownerCapsule);
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/DelegateResourceActuator.java (L64-98)
```java
    AccountCapsule ownerCapsule = accountStore
        .get(delegateResourceContract.getOwnerAddress().toByteArray());
    DynamicPropertiesStore dynamicStore = chainBaseManager.getDynamicPropertiesStore();
    long delegateBalance = delegateResourceContract.getBalance();
    boolean lock = delegateResourceContract.getLock();
    long lockPeriod = getLockPeriod(dynamicStore.supportMaxDelegateLockPeriod(),
            delegateResourceContract);
    byte[] receiverAddress = delegateResourceContract.getReceiverAddress().toByteArray();

    // delegate resource to receiver
    switch (delegateResourceContract.getResource()) {
      case BANDWIDTH:
        delegateResource(ownerAddress, receiverAddress, true,
            delegateBalance, lock, lockPeriod);

        ownerCapsule.addDelegatedFrozenV2BalanceForBandwidth(delegateBalance);
        ownerCapsule.addFrozenBalanceForBandwidthV2(-delegateBalance);
        break;
      case ENERGY:
        delegateResource(ownerAddress, receiverAddress, false,
            delegateBalance, lock, lockPeriod);

        ownerCapsule.addDelegatedFrozenV2BalanceForEnergy(delegateBalance);
        ownerCapsule.addFrozenBalanceForEnergyV2(-delegateBalance);
        break;
      default:
        logger.debug("Resource Code Error.");
    }

    accountStore.put(ownerCapsule.createDbKey(), ownerCapsule);

    ret.setStatus(fee, code.SUCESS);

    return true;
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/DelegateResourceActuator.java (L191-220)
```java
    byte[] receiverAddress = delegateResourceContract.getReceiverAddress().toByteArray();

    if (!DecodeUtil.addressValid(receiverAddress)) {
      throw new ContractValidateException("Invalid receiverAddress");
    }


    if (Arrays.equals(receiverAddress, ownerAddress)) {
      throw new ContractValidateException(
          "receiverAddress must not be the same as ownerAddress");
    }

    AccountCapsule receiverCapsule = accountStore.get(receiverAddress);
    if (receiverCapsule == null) {
      String readableOwnerAddress = StringUtil.createReadableString(receiverAddress);
      throw new ContractValidateException(
          ActuatorConstant.ACCOUNT_EXCEPTION_STR
              + readableOwnerAddress + NOT_EXIST_STR);
    }

    boolean lock = delegateResourceContract.getLock();
    if (lock && dynamicStore.supportMaxDelegateLockPeriod()) {
      long lockPeriod = getLockPeriod(true, delegateResourceContract);
      long maxDelegateLockPeriod = dynamicStore.getMaxDelegateLockPeriod();
      if (lockPeriod < 0 || lockPeriod > maxDelegateLockPeriod) {
        throw new ContractValidateException(
            "The lock period of delegate resource cannot be less than 0 and cannot exceed "
                + maxDelegateLockPeriod + "!");
      }

```

**File:** actuator/src/main/java/org/tron/core/actuator/UnDelegateResourceActuator.java (L75-80)
```java
          if (receiverCapsule.getAcquiredDelegatedFrozenV2BalanceForBandwidth()
              < unDelegateBalance) {
            // A TVM contract suicide, re-create will produce this situation
            receiverCapsule.setAcquiredDelegatedFrozenV2BalanceForBandwidth(0);
          } else {
            // calculate usage
```
