### Title
`MAX_DELEGATE_LOCK_PERIOD` committee parameter changes retroactively invalidate in-flight `DelegateResourceContract` transactions, unexpectedly blocking resource delegation - (File: actuator/src/main/java/org/tron/core/actuator/DelegateResourceActuator.java)

### Summary
`DelegateResourceActuator.validate()` bounds a user-chosen `lockPeriod` against the *current* value of the chain parameter `MAX_DELEGATE_LOCK_PERIOD`, which is stored in `DynamicPropertiesStore` and can be changed at any time by a committee proposal. Because the check is re-evaluated against the live value at the moment the transaction is validated (not the value in effect when the user built/signed the transaction), a routine, non-malicious parameter change that lowers `MAX_DELEGATE_LOCK_PERIOD` can cause a previously well-formed, broadcast `DelegateResourceContract` transaction to revert with a validation error, exactly mirroring the `timewindow` unexpected-change pattern in the external report.

### Finding Description
An unprivileged user builds a `DelegateResourceContract` with `lock=true` and a `lockPeriod` value that is valid under the `maxDelegateLockPeriod` that is queried and returned to clients (e.g. via the current chain parameter list) at the time of transaction construction. The actuator only checks this bound during validate, using the value read at execution time: [1](#0-0) 

`getMaxDelegateLockPeriod()` simply returns whatever is currently stored in `DynamicPropertiesStore`: [2](#0-1) 

This value is mutated by a committee proposal (`ProposalService.process`) as soon as the proposal is approved and processed at a maintenance cycle, with no relationship to any specific transaction's timing: [3](#0-2) 

Approved proposals are applied automatically by `ProposalController.processProposal` / `processProposals` during normal block/maintenance processing — there is no timelock or grace period for already-broadcast transactions that were built under the old parameter value: [4](#0-3) 

Additionally, `validRemainTime` compares the user's requested `lockPeriod` against the *previous* lock's remaining time using the current lock period value, which is likewise exposed to the same class of retroactive-parameter risk: [5](#0-4) 

The unit tests confirm the actuator enforces the bound strictly against whatever `maxDelegateLockPeriod` happens to be set at validate time: [6](#0-5) 

This is structurally identical to the reported Y2K Finance bug class: a governance-controlled parameter (`timewindow` / `MAX_DELEGATE_LOCK_PERIOD`) is compared against a live/broadcast transaction's user-chosen value, and any change to that parameter takes effect immediately for all future-validated transactions, including ones that were already fully formed and signed by the user under the old rules.

### Impact Explanation
A user who submits a `DelegateResourceContract` (locking bandwidth/energy delegation for up to the then-current maximum lock period) can have their transaction unexpectedly rejected if, between construction/broadcast and validation, the committee lowers `MAX_DELEGATE_LOCK_PERIOD` via a routine proposal that becomes effective at the next maintenance cycle. The result: the user's transaction fails validation ("The lock period of delegate resource cannot be less than 0 and cannot exceed ...!"), wasting the fee/bandwidth spent broadcasting the transaction and preventing an otherwise legitimate resource-delegation operation from completing — the same class of unfair, gas/fee-wasting denial described in the source report. Because delegation directly affects account resource allocation (bandwidth/energy available for further transactions), unpredictable failures here degrade the reliability of a commonly used resource-management feature for ordinary account holders.

### Likelihood Explanation
`MAX_DELEGATE_LOCK_PERIOD` is a standard governance-tunable chain parameter (adjustable via `ProposalCreateActuator`/committee voting) that is expected to be adjusted periodically as part of normal network operation, not only in adversarial scenarios. Any user submitting a locked delegation near the current maximum lock period window is at risk whenever such a parameter update lands during normal maintenance processing, making this a realistically reachable condition during regular chain operation rather than a contrived edge case.

### Recommendation
Snapshot/validate `lockPeriod` bounds using the parameter value that was in effect when the transaction was constructed (e.g., include an expected parameter version/epoch in the contract, or apply parameter changes only to transactions broadcast after a timelock/notice period), consistent with the original report's mitigation to avoid retroactively enforcing new governance parameters against already-signed user transactions.

### Proof of Concept
1. Freeze balance for `BANDWIDTH` for `OWNER_ADDRESS`, then observe current `dynamicStore.getMaxDelegateLockPeriod()` value (e.g., 864000).
2. User builds and signs a `DelegateResourceContract` with `lock=true`, `lockPeriod=800000` (valid under current max), analogous to `getMaxDelegateLockPeriodContractForBandwidth` in the test suite: [7](#0-6) 
3. Before the transaction is included/validated, a committee proposal lowers `MAX_DELEGATE_LOCK_PERIOD` to e.g. 500000 and it is processed via `ProposalService.process` at the next maintenance cycle: [3](#0-2) 
4. The user's transaction is then validated; `DelegateResourceActuator.validate()` now rejects it with `ContractValidateException("The lock period of delegate resource cannot be less than 0 and cannot exceed 500000!")`, matching the check at [8](#0-7) , even though the transaction was valid at construction/broadcast time.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/DelegateResourceActuator.java (L211-219)
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
```

**File:** actuator/src/main/java/org/tron/core/actuator/DelegateResourceActuator.java (L261-269)
```java
  private void validRemainTime(ResourceCode resourceCode, long lockPeriod, long expireTime,
      long now) throws ContractValidateException {
    long remainTime = expireTime - now;
    if (lockPeriod * BLOCK_PRODUCED_INTERVAL < remainTime) {
      throw new ContractValidateException(
          "The lock period for " + resourceCode.name() + " this time cannot be less than the "
              + "remaining time[" + remainTime + "ms] of the last lock period for "
              + resourceCode.name() + "!");
    }
```

**File:** chainbase/src/main/java/org/tron/core/store/DynamicPropertiesStore.java (L2865-2870)
```java
  public long getMaxDelegateLockPeriod() {
    return Optional.ofNullable(getUnchecked(MAX_DELEGATE_LOCK_PERIOD))
        .map(BytesCapsule::getData)
        .map(ByteArray::toLong)
        .orElse(DELEGATE_PERIOD / BLOCK_PRODUCED_INTERVAL);
  }
```

**File:** framework/src/main/java/org/tron/core/consensus/ProposalService.java (L355-358)
```java
        case MAX_DELEGATE_LOCK_PERIOD: {
          manager.getDynamicPropertiesStore().saveMaxDelegateLockPeriod(entry.getValue());
          break;
        }
```

**File:** framework/src/main/java/org/tron/core/consensus/ProposalController.java (L61-66)
```java
      long currentTime = manager.getDynamicPropertiesStore().getNextMaintenanceTime();
      if (proposalCapsule.hasExpired(currentTime)) {
        processProposal(proposalCapsule);
        proposalNum--;
        continue;
      }
```

**File:** framework/src/test/java/org/tron/core/actuator/DelegateResourceActuatorTest.java (L129-138)
```java
  private Any getMaxDelegateLockPeriodContractForBandwidth(long unfreezeBalance, long lockPeriod) {
    return Any.pack(DelegateResourceContract.newBuilder()
        .setOwnerAddress(ByteString.copyFrom(ByteArray.fromHexString(OWNER_ADDRESS)))
        .setReceiverAddress(ByteString.copyFrom(ByteArray.fromHexString(RECEIVER_ADDRESS)))
        .setBalance(unfreezeBalance)
        .setResource(BANDWIDTH)
        .setLock(true)
        .setLockPeriod(lockPeriod)
        .build());
  }
```

**File:** framework/src/test/java/org/tron/core/actuator/DelegateResourceActuatorTest.java (L444-456)
```java
  @Test
  public void testMaxDelegateLockPeriodForBandwidthWrongLockPeriod1() {
    dbManager.getDynamicPropertiesStore().saveMaxDelegateLockPeriod(86401);
    freezeBandwidthForOwner();
    long delegateBalance = 1_000_000_000L;
    DelegateResourceActuator actuator = new DelegateResourceActuator();
    actuator.setChainBaseManager(dbManager.getChainBaseManager()).setAny(
        getMaxDelegateLockPeriodContractForBandwidth(
            delegateBalance, 370 * 24 * 3600));
    assertThrows("The lock period of delegate resources cannot exceed 1 year!",
        ContractValidateException.class, actuator::validate);
    dbManager.getDynamicPropertiesStore().saveMaxDelegateLockPeriod(DELEGATE_PERIOD / 3000);
  }
```
