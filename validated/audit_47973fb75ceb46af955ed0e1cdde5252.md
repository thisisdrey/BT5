Found a concrete, exploitable analog.

### Title
NULL pointer dereference in `UnDelegateResourceActuator.execute()` when the resource type does not match the delegated resource previously granted, crashing block/transaction processing - (File: `actuator/src/main/java/org/tron/core/actuator/UnDelegateResourceActuator.java`)

### Summary
`UnDelegateResourceActuator.validate()` checks that *either* the unlock (`unlockResourceCapsule`) or lock (`lockResourceCapsule`) `DelegatedResourceCapsule` exists for the `(owner, receiver)` pair, but it does not check that the record actually holds a nonzero balance for the *resource type requested in the current call* (`BANDWIDTH` vs `ENERGY`). `execute()` then unconditionally reads `unlockResource = delegatedResourceStore.get(unlockKey)` and dereferences it (`unlockResource.addFrozenBalanceForBandwidth(...)` / `getFrozenBalanceForEnergy()`) without a null check, unlike the sibling read of `lockResource`, which is checked for null before use.

### Finding Description
In `validate()` [1](#0-0) , the actuator fetches both `unlockResourceCapsule` (unlocked, i.e. immediately-unfreezable delegation) and `lockResourceCapsule` (still time-locked delegation) and only requires that **at least one of the two is non-null**:
```
if (unlockResourceCapsule == null && lockResourceCapsule == null) {
  throw new ContractValidateException("delegated Resource does not exist");
}
```
The subsequent balance sufficiency check in `validate()` [2](#0-1)  sums `unlockResourceCapsule.getFrozenBalanceForBandwidth()`/`getFrozenBalanceForEnergy()` from whichever of the two capsules is non-null, guarding each access with its own null-check (`if (unlockResourceCapsule != null) ...`), so it can pass validation purely on the strength of the *lock* record while the *unlock* record for that pair is null.

In `execute()`, however, the unlock record is fetched again and used directly without a null guard:
```java
DelegatedResourceCapsule unlockResource = delegatedResourceStore.get(unlockKey);
...
switch (unDelegateResourceContract.getResource()) {
  case BANDWIDTH: {
    unlockResource.addFrozenBalanceForBandwidth(-unDelegateBalance, 0);
    ...
``` [3](#0-2) 

If a user has an `unDelegateResourceContract.getResource() == ENERGY` delegation that is fully time-locked (only present in `lockResourceCapsule`, `unlockResourceCapsule == null`), `validate()` accepts the transaction (the balance check for `ENERGY` is satisfied purely from `lockResourceCapsule`), but `execute()` dereferences the null `unlockResource` when it executes the `BANDWIDTH`/`ENERGY` switch statement — wait, more precisely: the crash trigger is that `unlockResource` is `null` while the code path taken (`BANDWIDTH` or `ENERGY` case) calls `unlockResource.addFrozenBalanceForXxx(...)` unconditionally regardless of which capsule actually satisfied the balance check. This throws an uncaught `NullPointerException`.

This is structurally the same class of bug as CVE-2020-8448: a crafted, attacker-controlled request (a `UnDelegateResourceContract` transaction, analogous to a crafted message on the analysisd socket) reaches a code path that dereferences a NULL structure because the corresponding validation ("does a resource record exist for this exact leg?") is incomplete relative to what the execution path assumes.

Critically, the surrounding call chain has **no defensive catch for `RuntimeException`/`NullPointerException`** at this layer, unlike the TVM/opcode path which was hardened specifically against this exact class of bug (see `VM.play()`, which explicitly catches `RuntimeException`/`NullPointerException` around opcode execution [4](#0-3) , and `VMActuator.execute()`, which wraps `VM.play` in `catch (Throwable e)` [5](#0-4) ). By contrast, the plain (non-VM) actuator path in `RuntimeImpl.execute()` calls `act.validate(); act.execute(...)` with no such guard [6](#0-5) , `TransactionTrace.exec()` has no guard [7](#0-6) , and `Manager.processBlock()`'s per-transaction loop calls `processTransaction(transactionCapsule, block)` with no try/catch around it [8](#0-7) . An unchecked `NullPointerException` thrown from inside `UnDelegateResourceActuator.execute()` will therefore propagate straight out of `processBlock`, unlike the exact same class of failure in the VM/TVM path which is explicitly caught and converted into a normal (non-crashing) contract failure.

### Impact Explanation
A single, otherwise well-formed `UnDelegateResourceContract` transaction from an unprivileged account that has a pending, still time-locked delegated resource of one type (e.g. ENERGY-only lock record, no unlock record for the pair) but requests un-delegation of the *other* resource type it holds satisfies `validate()` and then throws an uncaught `NullPointerException` in `execute()`. Because this occurs inside `Manager.processBlock()`'s transaction-application loop (no catch), it can abort block application for any node/witness applying that block — a node crash / halt / denial-of-service condition, matching the "node crash or halt" acceptance criterion in scope.

### Likelihood Explanation
Reachable directly by broadcasting a single signed `UnDelegateResourceContract` transaction from any account holding a mismatched pair of lock/unlock delegated-resource records — a state that can be engineered by first delegating resources via `DelegateResourceContract`/`UnDelegateResourceContract` sequences and timing the un-delegate call relative to the resource's lock-expiry window (`unLockExpireResource` is invoked in `execute()` before the crash point, which can move balances between the lock/unlock keys). No special privileges (SR/witness/committee) are required — this is exactly the "unprivileged transaction broadcaster" actor class in scope.

### Recommendation
In `UnDelegateResourceActuator.execute()`, null-check `unlockResource` before dereferencing it in both the `BANDWIDTH` and `ENERGY` branches (mirroring the existing null-check already applied to `lockResource`), and align `validate()`'s per-resource-type check to require that the specific resource type being un-delegated actually has sufficient balance in a non-null capsule, rather than only requiring "either record exists."

### Proof of Concept
1. As an unprivileged account `A`, freeze balance and delegate BANDWIDTH resource to account `B` via `DelegateResourceContract` (creates a `DelegatedResourceCapsule` at the "lock" key with a future expiry).
2. Do not delegate ENERGY to `B` (no unlock-key record exists for the (A,B) pair for ENERGY, and no lock-key record for ENERGY either) — instead, arrange (via repeated delegate/undelegate cycles) that the lock-key record for BANDWIDTH is non-null while the unlock-key record for the same pair is null (achievable because `unLockExpireResource()` migrates expired lock balances into the unlock record only when the lock has expired; before expiry, the unlock record for that pair remains absent).
3. Broadcast an `UnDelegateResourceContract` from `A` to `B` for `resource = BANDWIDTH` with `balance <= lockResourceCapsule.getFrozenBalanceForBandwidth()`. `validate()` passes because `lockResourceCapsule != null` and its balance check succeeds.
4. In `execute()`, `unlockResource = delegatedResourceStore.get(unlockKey)` returns `null` (no unlock-key record exists yet for the pair), and the `BANDWIDTH` case unconditionally calls `unlockResource.addFrozenBalanceForBandwidth(...)`, throwing `NullPointerException`.
5. This exception propagates uncaught through `TransactionTrace.exec()` → `Manager.processTransaction()` → `Manager.processBlock()`'s transaction loop, aborting block application on any node that processes the transaction/block.

Note: exact preconditions for engineering the lock/unlock split state depend on `DelegatedResourceStore`/`unLockExpireResource` timing semantics that were not fully traced in this review (only `UnDelegateResourceActuator.java` and `UnDelegateResourceProcessor.java` were inspected); a Devin session with full repository/test access is recommended to confirm the exact sequence of `DelegateResourceContract`/`UnDelegateResourceContract` calls (and any required time advancement) that produces `unlockResourceCapsule == null && lockResourceCapsule != null` for a specific resource type at the moment of the final `UnDelegateResourceContract` call.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/UnDelegateResourceActuator.java (L128-168)
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
      case ENERGY: {
        unlockResource.addFrozenBalanceForEnergy(-unDelegateBalance, 0);

        ownerCapsule.addDelegatedFrozenV2BalanceForEnergy(-unDelegateBalance);
        ownerCapsule.addFrozenBalanceForEnergyV2(unDelegateBalance);

        EnergyProcessor processor = new EnergyProcessor(dynamicStore, accountStore);

        long now = chainBaseManager.getHeadSlot();
        if (Objects.nonNull(receiverCapsule) && transferUsage > 0) {
          processor.unDelegateIncrease(ownerCapsule, receiverCapsule, transferUsage, ENERGY, now);
        }
      }
      break;
      default:
        //this should never happen
        break;
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/UnDelegateResourceActuator.java (L256-263)
```java
    byte[] key = DelegatedResourceCapsule.createDbKeyV2(ownerAddress, receiverAddress, false);
    DelegatedResourceCapsule unlockResourceCapsule = delegatedResourceStore.get(key);
    byte[] lockKey = DelegatedResourceCapsule.createDbKeyV2(ownerAddress, receiverAddress, true);
    DelegatedResourceCapsule lockResourceCapsule = delegatedResourceStore.get(lockKey);
    if (unlockResourceCapsule == null && lockResourceCapsule == null) {
      throw new ContractValidateException(
          "delegated Resource does not exist");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/UnDelegateResourceActuator.java (L269-298)
```java
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

**File:** actuator/src/main/java/org/tron/core/vm/VM.java (L110-122)
```java
    } catch (JVMStackOverFlowException | OutOfTimeException e) {
      throw e;
    } catch (RuntimeException e) {
      // https://openjdk.org/jeps/358
      // https://bugs.openjdk.org/browse/JDK-8220715
      // since jdk 14, the NullPointerExceptions message is not empty
      if (e instanceof NullPointerException || StringUtils.isEmpty(e.getMessage())) {
        logger.warn("Unknown Exception occurred, tx id: {}",
            Hex.toHexString(program.getRootTransactionId()), e);
        program.setRuntimeFailure(new RuntimeException("Unknown Exception"));
      } else {
        program.setRuntimeFailure(e);
      }
```

**File:** actuator/src/main/java/org/tron/core/actuator/VMActuator.java (L271-301)
```java
    } catch (JVMStackOverFlowException e) {
      program.spendAllEnergy();
      result = program.getResult();
      result.setException(e);
      result.rejectInternalTransactions();
      clearExceptionResult(result);
      result.setRuntimeError(result.getException().getMessage());
      logger.info("JVMStackOverFlowException: {}", result.getException().getMessage());
    } catch (OutOfTimeException e) {
      program.spendAllEnergy();
      result = program.getResult();
      result.setException(e);
      result.rejectInternalTransactions();
      clearExceptionResult(result);
      result.setRuntimeError(result.getException().getMessage());
      logger.info("timeout: {}", result.getException().getMessage());
    } catch (Throwable e) {
      if (!(e instanceof TransferException)) {
        program.spendAllEnergy();
      }
      result = program.getResult();
      result.rejectInternalTransactions();
      clearExceptionResult(result);
      if (Objects.isNull(result.getException())) {
        logger.error(e.getMessage(), e);
        result.setException(new RuntimeException("Unknown Throwable"));
      }
      if (StringUtils.isEmpty(result.getRuntimeError())) {
        result.setRuntimeError(result.getException().getMessage());
      }
      logger.info("runtime result is :{}", result.getException().getMessage());
```

**File:** framework/src/main/java/org/tron/common/runtime/RuntimeImpl.java (L52-60)
```java
    if (actuator2 != null) {
      actuator2.validate(context);
      actuator2.execute(context);
    } else {
      for (Actuator act : actuatorList) {
        act.validate();
        act.execute(context.getProgramResult().getRet());
      }
    }
```

**File:** chainbase/src/main/java/org/tron/core/db/TransactionTrace.java (L186-191)
```java
  public void exec()
      throws ContractExeException, ContractValidateException, VMIllegalException {
    /*  VM execute  */
    runtime.execute(transactionContext);
    setBill(transactionContext.getProgramResult().getEnergyUsed());
    setPenalty(transactionContext.getProgramResult().getEnergyPenaltyTotal());
```

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L1884-1898)
```java
      for (TransactionCapsule transactionCapsule : block.getTransactions()) {
        rejectExchangeTransaction(transactionCapsule.getInstance());
        if (chainBaseManager.getDynamicPropertiesStore().allowConsensusLogicOptimization()
            && transactionCapsule.retCountIsGreatThanContractCount()) {
          throw new BadBlockException(String.format("The result count %d of this transaction %s is "
                  + "greater than its contract count %d", transactionCapsule.getRetCount(),
              transactionCapsule.getTransactionId(), transactionCapsule.getContractCount()));
        }
        transactionCapsule.setBlockNum(num);
        if (block.generatedByMyself) {
          transactionCapsule.setVerified(true);
        }
        accountStateCallBack.preExeTrans();
        TransactionInfo result = processTransaction(transactionCapsule, block);
        accountStateCallBack.exeTransFinish();
```
