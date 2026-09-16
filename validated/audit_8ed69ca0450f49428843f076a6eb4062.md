## Title
Null Pointer Dereference Crash in `UnDelegateResourceActuator.execute()` via Locked-Only Delegation Undelegation — (File: `actuator/src/main/java/org/tron/core/actuator/UnDelegateResourceActuator.java`)

### Summary
`UnDelegateResourceActuator` allows any account to reclaim ("undelegate") resources it previously delegated via `DelegateResourceContract`/`UnDelegateResourceContract`. The `validate()` method treats the "unlocked" delegation record and the "locked" (time-locked) delegation record as interchangeable when computing available balance, but `execute()` unconditionally dereferences the "unlocked" record even when it does not exist in the store. This is analogous to the Asterisk CVE-2019-15297 pattern, where a state object expected by the caller (a `session media` object created by the party that initiated the flow) can be `NULL` on an unexpected/atypical branch, and the response-handling code dereferences it without a null check, crashing the process.

### Finding Description
In validate: [1](#0-0) 

`unlockResourceCapsule` and `lockResourceCapsule` are independently nullable, and either one being non-null (and, for the lock resource, expired) is sufficient to satisfy the balance check:
```
if (unlockResourceCapsule == null && lockResourceCapsule == null) {
  throw new ContractValidateException("delegated Resource does not exist");
}
...
long delegateBalance = 0;
if (unlockResourceCapsule != null) {
  delegateBalance += unlockResourceCapsule.getFrozenBalanceForBandwidth();
}
if (lockResourceCapsule != null && lockResourceCapsule.getExpireTimeForBandwidth() < now) {
  delegateBalance += lockResourceCapsule.getFrozenBalanceForBandwidth();
}
```
So validation succeeds if only a **locked** delegation record exists (no "unlocked" record was ever created) and its lock period has expired.

In execute, however, the code fetches only the "unlocked" record and unconditionally dereferences it, with no null check: [2](#0-1) 

```
byte[] unlockKey = DelegatedResourceCapsule.createDbKeyV2(ownerAddress, receiverAddress, false);
DelegatedResourceCapsule unlockResource = delegatedResourceStore.get(unlockKey);
...
switch (unDelegateResourceContract.getResource()) {
  case BANDWIDTH: {
    unlockResource.addFrozenBalanceForBandwidth(-unDelegateBalance, 0);
    ...
```
If `unlockResource` is `null` (which `validate()` permits), this throws a `NullPointerException`, an unchecked `RuntimeException` not caught by the actuator's `catch (InvalidProtocolBufferException e)` block, propagating out of `execute()` during transaction/block processing.

### Impact Explanation
`UnDelegateResourceActuator.execute()` runs deterministically for every node applying the block containing the malicious transaction (in `Manager`'s block-application/transaction-processing path). Because the bug is deterministic and reachable via a single signed, unprivileged transaction, it can crash the node process (uncaught NPE) or, depending on how the surrounding transaction pipeline swallows/propagates the exception, cause inconsistent processing between nodes — leading to denial of service or potential chain split/halt across the network, matching the "node crash or halt, chain split" acceptance criteria.

### Likelihood Explanation
Any account holder who has previously created a **locked** delegation (`DelegateResourceContract` with `lock=true`) to a receiver, and never created a separate unlocked delegation to that same receiver, can trigger this once the lock period expires by broadcasting a plain `UnDelegateResourceContract` for that receiver/resource. This requires no special privilege, witness/SR status, or victim cooperation — only a single self-authored transaction sequence (delegate-locked → wait for expiry → undelegate).

### Recommendation
In `UnDelegateResourceActuator.execute()`, null-check `unlockResource` before dereferencing it (mirroring the null-safety already present for `receiverCapsule`), and instead operate on `lockResource` when `unlockResource` is null and the lock has expired — consistent with how `validate()` computes `delegateBalance`. The analogous native-contract path `UnDelegateResourceProcessor.execute()` (`actuator/src/main/java/org/tron/core/vm/nativecontract/UnDelegateResourceProcessor.java`, lines ~160-208) should be audited for the same divergence between its `validate()` and `execute()` logic.

### Proof of Concept
1. Account A calls `DelegateResourceContract` with `lock=true`, delegating BANDWIDTH to account B for a short lock period (creates only the "locked" `DelegatedResourceCapsule`, key suffix `true`, at [3](#0-2) ; no "unlocked" record, key suffix `false`, is ever created for this receiver).
2. Wait until the lock period expires (`lockResourceCapsule.getExpireTimeForBandwidth() < now`).
3. Account A broadcasts `UnDelegateResourceContract` for the same receiver/resource with `balance <= lockResourceCapsule.getFrozenBalanceForBandwidth()`.
4. `validate()` (lines 256-283) succeeds because `lockResourceCapsule` alone covers `delegateBalance`.
5. `execute()` (lines 128-137) fetches `unlockResource = delegatedResourceStore.get(unlockKey)` → `null`, then calls `unlockResource.addFrozenBalanceForBandwidth(...)` → `NullPointerException`, propagating uncaught out of actuator execution during block application.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/UnDelegateResourceActuator.java (L128-150)
```java
    byte[] unlockKey = DelegatedResourceCapsule
        .createDbKeyV2(ownerAddress, receiverAddress, false);
    DelegatedResourceCapsule unlockResource = delegatedResourceStore
        .get(unlockKey);

    // modify owner Account
    AccountCapsule ownerCapsule = accountStore.get(ownerAddress);
    switch (unDelegateResourceContract.getResource()) {
      case BANDWIDTH: {
        unlockResource.addFrozenBalanceForBandwidth(-unDelegateBalance, 0);

        ownerCapsule.addDelegatedFrozenV2BalanceForBandwidth(-unDelegateBalance);
        ownerCapsule.addFrozenBalanceForBandwidthV2(unDelegateBalance);

        BandwidthProcessor processor = new BandwidthProcessor(chainBaseManager);

        long now = chainBaseManager.getHeadSlot();
        if (Objects.nonNull(receiverCapsule) && transferUsage > 0) {
          processor.unDelegateIncrease(ownerCapsule, receiverCapsule,
              transferUsage, BANDWIDTH, now);
        }
      }
      break;
```

**File:** actuator/src/main/java/org/tron/core/actuator/UnDelegateResourceActuator.java (L256-283)
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
```

**File:** actuator/src/main/java/org/tron/core/actuator/DelegateResourceActuator.java (L74-81)
```java
    switch (delegateResourceContract.getResource()) {
      case BANDWIDTH:
        delegateResource(ownerAddress, receiverAddress, true,
            delegateBalance, lock, lockPeriod);

        ownerCapsule.addDelegatedFrozenV2BalanceForBandwidth(delegateBalance);
        ownerCapsule.addFrozenBalanceForBandwidthV2(-delegateBalance);
        break;
```
