### Title
Inconsistent resource-lock validation between `UnDelegateResourceActuator` and TVM `UnDelegateResourceProcessor` allows bypassing lock-period accounting - (File: `actuator/src/main/java/org/tron/core/vm/nativecontract/UnDelegateResourceProcessor.java`)

### Summary
Similar to the FraxLocker pattern where a broad, less-restricted entry point (`execute`) can replicate a narrower, more-restricted function (`claimFXSRewards`) and thereby bypass its stricter checks, java-tron exposes two independent code paths for the exact same state-changing operation — un-delegating a TRX resource delegation (bandwidth/energy) — with materially different validation logic: the actuator path (`UnDelegateResourceActuator`, reached via a signed `UnDelegateResourceContract` transaction) and the TVM opcode path (`UnDelegateResourceProcessor`, reached from inside any smart contract via the `UNDELEGATERESOURCE` TVM instruction, callable by any contract deployer/caller).

### Finding Description
`UnDelegateResourceActuator.validate()` checks **both** the unlocked delegation record (key with `lock=false`) and the locked delegation record (key with `lock=true`), and only counts a locked record's balance toward the available amount if `lockResourceCapsule.getExpireTime...() < now` — i.e. only if the lock has actually expired: [1](#0-0) 

By contrast, `UnDelegateResourceProcessor.validate()` (invoked from `Program.unDelegateResource`, i.e. the `UNDELEGATERESOURCE` TVM opcode reachable by any contract call) only fetches the **unlocked** key (`lock=false`) and validates the requested amount solely against that single record, never inspecting the locked record at all: [2](#0-1) 

This is invoked from `Program.unDelegateResource`: [3](#0-2) 

Because a contract's own address can be an `ownerAddress` for a delegation (delegation is done via `Program.delegateResource`/`DelegateResourceActuator`, and a smart-contract account can itself be the delegator), and because `UnDelegateResourceProcessor.execute()` unconditionally operates on the unlocked key (`createDbKeyV2(ownerAddress, receiverAddress, false)`), the TVM path never triggers `delegatedResourceStore.unLockExpireResource(...)` (which the actuator path calls before reading the unlock key) and never validates against the locked/expiry-gated balance the way the actuator does. Both paths ultimately write to the same `DelegatedResourceStore`/`AccountCapsule` structures, so the TVM opcode is functionally capable of "replicating" the actuator's operation but with a strictly weaker/incomplete precondition set — exactly the FraxLocker `execute`-vs-`claimFXSRewards` inconsistency pattern: one entry point (actuator) enforces the intended lock-expiry semantics, the other (opcode, reachable from arbitrary contract bytecode) does not.

### Impact Explanation
If a locked delegated resource has not yet expired, the actuator refuses to count it toward `unDelegateBalance` (protecting the lock guarantee). The TVM processor path does not check locked records at all, so if any state combination allows an unlocked-key record to under- or over-represent the true un-lockable balance (e.g., timing/ordering differences between when `unLockExpireResource` migrates expired-locked funds into the unlocked bucket), a contract-driven un-delegate could diverge from the actuator's accounting rules, resulting in resource-delegation/reward and bandwidth/energy accounting inconsistency between the two entry points for what should be one canonical operation. This is a state-integrity/inconsistent-access-control class issue rather than a directly demonstrated fund-theft primitive from the available code alone.

### Likelihood Explanation
The TVM `UNDELEGATERESOURCE` opcode is reachable by any contract deployer/caller once `allowTvmFreezeV2`/relevant VM config is enabled (a standard, broadcastable `TriggerSmartContract` transaction), making the weaker path trivially reachable by an unprivileged actor without any special permission beyond normal contract interaction.

### Recommendation
Align `UnDelegateResourceProcessor.validate()`/`execute()` with `UnDelegateResourceActuator`: check both the locked (`lock=true`) and unlocked (`lock=false`) `DelegatedResourceCapsule` records, apply the same expiry-gating logic (`getExpireTimeForBandwidth/Energy() < now`) before including locked balances, and call `unLockExpireResource` prior to reading the unlock key, so both entry points enforce identical un-delegation preconditions.

### Proof of Concept
Not independently verified end-to-end (would require constructing a contract that delegates resources to itself/another contract, waits across lock-expiry boundaries, and calls the `UNDELEGATERESOURCE` opcode versus a normal `UnDelegateResourceContract` transaction to diff outcomes); this is flagged as a code-path/logic inconsistency identified via static review of `UnDelegateResourceActuator.validate()` vs `UnDelegateResourceProcessor.validate()`, not a confirmed exploited fund-loss scenario.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/UnDelegateResourceActuator.java (L256-298)
```java
    byte[] key = DelegatedResourceCapsule.createDbKeyV2(ownerAddress, receiverAddress, false);
    DelegatedResourceCapsule unlockResourceCapsule = delegatedResourceStore.get(key);
    byte[] lockKey = DelegatedResourceCapsule.createDbKeyV2(ownerAddress, receiverAddress, true);
    DelegatedResourceCapsule lockResourceCapsule = delegatedResourceStore.get(lockKey);
    if (unlockResourceCapsule == null && lockResourceCapsule == null) {
      throw new ContractValidateException(
          "delegated Resource does not exist");
    }

    long unDelegateBalance = unDelegateResourceContract.getBalance();
    if (unDelegateBalance <= 0) {
      throw new ContractValidateException("unDelegateBalance must be more than 0 TRX");
    }
    switch (unDelegateResourceContract.getResource()) {
      case BANDWIDTH: {
        long delegateBalance = 0;
        if (unlockResourceCapsule != null) {
          delegateBalance += unlockResourceCapsule.getFrozenBalanceForBandwidth();
        }
        if (lockResourceCapsule != null
            && lockResourceCapsule.getExpireTimeForBandwidth() < now) {
          delegateBalance += lockResourceCapsule.getFrozenBalanceForBandwidth();
        }
        if (delegateBalance < unDelegateBalance) {
          throw new ContractValidateException(
              "insufficient delegatedFrozenBalance(BANDWIDTH), request="
                  + unDelegateBalance + ", unlock_balance=" + delegateBalance);
        }
      }
      break;
      case ENERGY: {
        long delegateBalance = 0;
        if (unlockResourceCapsule != null) {
          delegateBalance += unlockResourceCapsule.getFrozenBalanceForEnergy();
        }
        if (lockResourceCapsule != null
            && lockResourceCapsule.getExpireTimeForEnergy() < now) {
          delegateBalance += lockResourceCapsule.getFrozenBalanceForEnergy();
        }
        if (delegateBalance < unDelegateBalance) {
          throw new ContractValidateException("insufficient delegateFrozenBalance(Energy), request="
              + unDelegateBalance + ", unlock_balance=" + delegateBalance);
        }
```

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/UnDelegateResourceProcessor.java (L60-88)
```java

    byte[] key = DelegatedResourceCapsule.createDbKeyV2(ownerAddress, receiverAddress, false);
    DelegatedResourceCapsule delegatedResourceCapsule = repo.getDelegatedResource(key);
    if (delegatedResourceCapsule == null) {
      throw new ContractValidateException(
          "delegated Resource does not exist");
    }

    long unDelegateBalance = param.getUnDelegateBalance();
    if (unDelegateBalance <= 0) {
      throw new ContractValidateException("unDelegateBalance must be more than 0 TRX");
    }
    switch (param.getResourceType()) {
      case BANDWIDTH:
        if (delegatedResourceCapsule.getFrozenBalanceForBandwidth() < unDelegateBalance) {
          throw new ContractValidateException("insufficient delegatedFrozenBalance(BANDWIDTH), request="
              + unDelegateBalance + ", balance=" + delegatedResourceCapsule.getFrozenBalanceForBandwidth());
        }
        break;
      case ENERGY:
        if (delegatedResourceCapsule.getFrozenBalanceForEnergy() < unDelegateBalance) {
          throw new ContractValidateException("insufficient delegateFrozenBalance(ENERGY), request="
              + unDelegateBalance + ", balance=" + delegatedResourceCapsule.getFrozenBalanceForEnergy());
        }
        break;
      default:
        throw new ContractValidateException(
            "Unknown ResourceCode, valid ResourceCode[BANDWIDTH、ENERGY]");
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L2202-2234)
```java
  public boolean unDelegateResource(
      DataWord receiverAddress, DataWord unDelegateBalance, DataWord resourceType) {
    Repository repository = getContractState().newRepositoryChild();
    byte[] owner = getContextAddress();
    byte[] receiver = receiverAddress.toTronAddress();

    increaseNonce();
    InternalTransaction internalTx = addInternalTx(null, owner, receiver,
        unDelegateBalance.longValue(), null,
        "unDelegateResourceOf" + convertResourceToString(resourceType), nonce, null);

    try {
      UnDelegateResourceParam param = new UnDelegateResourceParam();
      param.setOwnerAddress(owner);
      param.setReceiverAddress(receiver);
      param.setUnDelegateBalance(unDelegateBalance.sValue().longValueExact());
      param.setResourceType(parseResourceCodeV2(resourceType));

      UnDelegateResourceProcessor processor = new UnDelegateResourceProcessor();
      processor.validate(param, repository);
      processor.execute(param, repository);
      repository.commit();
      return true;
    } catch (ContractValidateException e) {
      logger.warn("TVM UnDelegateResource: validate failure. Reason: {}", e.getMessage());
    } catch (ArithmeticException e) {
      logger.warn("TVM UnDelegateResource: balance out of long range.");
    }
    if (internalTx != null) {
      internalTx.reject();
    }
    return false;
  }
```
