### Title
Missing self-destruct check in FreezeBalanceProcessor (V1 TVM native freeze) allows post-SELFDESTRUCT resource freezing analogous to f2fs's missing atomic_file check - (`File: actuator/src/main/java/org/tron/core/vm/nativecontract/FreezeBalanceProcessor.java`)

### Summary
The CVE describes f2fs ioctl handlers (`f2fs_ioc_set_pin_file`, `f2fs_move_file_range`, `f2fs_defragment_range`) that mutate special-mode file state without checking a concurrently-set `atomic_file` flag, letting an unprivileged caller race a state-changing operation against a mode the kernel assumed was exclusive. The java-tron analog is a state-guard that was added to only one of several structurally identical native "stake" contracts reachable from TVM opcodes: `FreezeBalanceV2Processor.validate()` explicitly checks `repo.isSelfDestructed(ownerAddress)` before allowing a freeze to proceed [1](#0-0) , but the legacy V1 `FreezeBalanceProcessor.validate()` performs no equivalent check [2](#0-1) , nor does `UnfreezeBalanceProcessor`, `DelegateResourceProcessor`, or `UnDelegateResourceProcessor` (no `isSelfDestructed` reference found in any of these files) [3](#0-2) .

### Finding Description
The precompiled/native-contract layer in the TVM (`org.tron.core.vm.nativecontract`) exposes stake/freeze/delegate operations that a smart contract can invoke via opcode during execution. When a contract calls `SELFDESTRUCT`, `Repository.markSelfDestruct()` records the address in `selfDestructCache`, and `isSelfDestructed()` reports that state for the remainder of the transaction (execution continues after SELFDESTRUCT until the call frame returns, per current EVM semantics preserved by java-tron's `Program`/`RepositoryImpl`) [4](#0-3) .

Because of this, java-tron developers already recognized the hazard and added a fork-gated guard specifically to `FreezeBalanceV2Processor`, calling `MUtil.checkCPUTimeForFreezeV2AfterSelfDestruct()` when `repo.isSelfDestructed(ownerAddress)` is true post-fork `VERSION_4_8_2_2` [1](#0-0) [5](#0-4) . This is confirmed as an intentional fix, since a dedicated test (`StakeV2AfterSelfDestructTest.freezeAfterSelfDestructIsForkGated`) exercises exactly this scenario and asserts the timeout is thrown once the fork is active [6](#0-5) .

However, the identical class of native contracts that predate V2 - `FreezeBalanceProcessor` (legacy freeze) and `UnfreezeBalanceProcessor` (legacy unfreeze/delegate teardown), along with `DelegateResourceProcessor`/`UnDelegateResourceProcessor` - have no such check anywhere in their `validate()` methods [2](#0-1) [3](#0-2) . This mirrors the f2fs bug class precisely: several sibling entry points into the same underlying "atomic/self-destruct-locked" state exist, and the state check was patched into only some of them, leaving the others as an unguarded race window.

### Impact Explanation
A malicious contract could, within a single transaction, execute `SELFDESTRUCT` and then continue executing further bytecode in the same call frame that invokes the legacy `FreezeBalanceProcessor`/`UnfreezeBalanceProcessor`/`DelegateResourceProcessor` native contracts against its own address (which is marked self-destructed but not yet removed from `AccountStore`, since deletion via `RepositoryImpl.deleteContract()` happens at commit time) [7](#0-6) . This can move/freeze/delegate TRX balance and resource weight tied to an account that is simultaneously scheduled for deletion, potentially producing state inconsistent with the invariant the V2 fix was designed to prevent — i.e., frozen/delegated balances that become unreachable or double-accounted once the self-destructed account and its code/storage are purged. This falls into the "permanent freezing of funds / unbacked balance state" impact category.

### Likelihood Explanation
Reachability is high in principle — TVM native contracts are directly invokable by any deployed smart contract via opcode from an unprivileged transaction sender — but exploitability depends on whether the legacy V1 freeze/delegate opcodes remain enabled for new contract deployments on the current network configuration (V1 freeze has been largely superseded by V2 under `supportUnfreezeDelay`/`dynamicStore` flags). This governs whether the missing check is still practically reachable on mainnet, which I could not fully confirm from the indexed code alone.

### Recommendation
Add the same `repo.isSelfDestructed(ownerAddress)` guard (and corresponding `MUtil.checkCPUTimeFor...AfterSelfDestruct()`-style fork-gated rejection) to `FreezeBalanceProcessor.validate()`, `UnfreezeBalanceProcessor.validate()`, `DelegateResourceProcessor.validate()`, and `UnDelegateResourceProcessor.validate()`, mirroring the fix already applied to `FreezeBalanceV2Processor`, so that all native "stake" entry points consistently reject execution against a self-destructed account within the same transaction.

### Proof of Concept
1. Deploy a contract `Attacker` that, in a single external call: (a) executes the legacy freeze native-contract call path (via the opcode/precompile that routes to `FreezeBalanceProcessor`) to freeze balance to itself, then (b) executes `SELFDESTRUCT(beneficiary)`, then (c) — if execution continues in the same frame or via a subsequent internal call before frame exit — invokes the legacy unfreeze/delegate path again.
2. Observe that `FreezeBalanceProcessor.validate()`/`UnfreezeBalanceProcessor.validate()` never queries `repo.isSelfDestructed(ownerAddress)`, unlike `FreezeBalanceV2Processor.validate()` at [1](#0-0) , so the legacy path succeeds where the V2 path would throw `OutOfTimeException`.
3. Compare resulting `AccountStore`/`DelegatedResourceStore` state after the self-destructed account is purged by `RepositoryImpl.deleteContract()` to confirm frozen/delegated balances are left in an inconsistent state relative to what the V2 guard was built to prevent.

Note: I could not fully verify from the index whether the legacy (V1) freeze/delegate TVM native-contract call paths are still reachable/enabled on the current mainnet configuration, since that depends on runtime opcode-gating logic not fully covered by my search. A Devin session with full repo/checkout access would be needed to trace the opcode dispatch table and confirm live reachability.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/FreezeBalanceV2Processor.java (L68-70)
```java
    if (repo.isSelfDestructed(ownerAddress)) {
      MUtil.checkCPUTimeForFreezeV2AfterSelfDestruct();
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/FreezeBalanceProcessor.java (L21-71)
```java
  public void validate(FreezeBalanceParam param, Repository repo) throws ContractValidateException {
    if (repo == null) {
      throw new ContractValidateException(STORE_NOT_EXIST);
    }

    // validate arg @frozenBalance
    byte[] ownerAddress = param.getOwnerAddress();
    AccountCapsule ownerCapsule = repo.getAccount(ownerAddress);
    long frozenBalance = param.getFrozenBalance();
    if (frozenBalance <= 0) {
      throw new ContractValidateException("FrozenBalance must be positive");
    } else if (frozenBalance < TRX_PRECISION) {
      throw new ContractValidateException("FrozenBalance must be greater than or equal to 1 TRX");
    } else if (frozenBalance > ownerCapsule.getBalance()) {
      throw new ContractValidateException("FrozenBalance must be less than or equal to accountBalance");
    }

    // validate frozen count of owner account
    int frozenCount = ownerCapsule.getFrozenCount();
    if (frozenCount != 0 && frozenCount != 1) {
      throw new ContractValidateException("FrozenCount must be 0 or 1");
    }

    // validate arg @resourceType
    switch (param.getResourceType()) {
      case BANDWIDTH:
      case ENERGY:
        break;
      default:
        throw new ContractValidateException(
            "Unknown ResourceCode, valid ResourceCode[BANDWIDTH、ENERGY]");
    }

    // validate for delegating resource
    byte[] receiverAddress = param.getReceiverAddress();
    if (!FastByteComparisons.isEqual(ownerAddress, receiverAddress)) {
      param.setDelegating(true);

      // check if receiver account exists. if not, then create a new account
      AccountCapsule receiverCapsule = repo.getAccount(receiverAddress);
      if (receiverCapsule == null) {
        receiverCapsule = repo.createNormalAccount(receiverAddress);
      }

      // forbid delegating resource to contract account
      if (receiverCapsule.getType() == Protocol.AccountType.Contract) {
        throw new ContractValidateException(
            "Do not allow delegate resources to contract addresses");
      }
    }
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/UnfreezeBalanceProcessor.java (L23-30)
```java
public class UnfreezeBalanceProcessor {

  public void validate(UnfreezeBalanceParam param, Repository repo)
      throws ContractValidateException {
    if (repo == null) {
      throw new ContractValidateException(STORE_NOT_EXIST);
    }

```

**File:** actuator/src/main/java/org/tron/core/vm/repository/RepositoryImpl.java (L487-492)
```java
  @Override
  public void deleteContract(byte[] address) {
    getCodeStore().delete(address);
    getAccountStore().delete(address);
    getContractStore().delete(address);
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/repository/RepositoryImpl.java (L576-597)
```java
  @Override
  public void markSelfDestruct(byte[] address) {
    selfDestructCache.add(Key.create(address));
  }

  @Override
  public boolean isSelfDestructed(byte[] address) {
    Key key = Key.create(address);
    if (selfDestructCache.contains(key)) {
      return true;
    }

    if (parent != null) {
      boolean isSelfDestructed = parent.isSelfDestructed(address);
      if (isSelfDestructed) {
        selfDestructCache.add(key);
      }
      return isSelfDestructed;
    } else {
      return false;
    }
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/utils/MUtil.java (L86-90)
```java
  public static void checkCPUTimeForFreezeV2AfterSelfDestruct() {
    if (ForkController.instance().pass(Parameter.ForkBlockVersionEnum.VERSION_4_8_2_2)) {
      throw new OutOfTimeException("CPU timeout for FreezeBalanceV2 after SELFDESTRUCT");
    }
  }
```

**File:** framework/src/test/java/org/tron/core/vm/nativecontract/StakeV2AfterSelfDestructTest.java (L33-60)
```java
  @Test
  public void freezeAfterSelfDestructIsForkGated() throws Exception {
    byte[] ownerAddress = address(1);
    AccountCapsule owner = account(ownerAddress, 0, 0);
    owner.setBalance(TRX_PRECISION);

    Repository repository = mock(Repository.class);
    DynamicPropertiesStore dynamicStore = mock(DynamicPropertiesStore.class);
    when(repository.getDynamicPropertiesStore()).thenReturn(dynamicStore);
    when(repository.getAccount(ownerAddress)).thenReturn(owner);
    when(repository.isSelfDestructed(ownerAddress)).thenReturn(true);

    FreezeBalanceV2Param param = new FreezeBalanceV2Param();
    param.setOwnerAddress(ownerAddress);
    param.setFrozenBalance(TRX_PRECISION);
    param.setResourceType(BANDWIDTH);
    FreezeBalanceV2Processor processor = new FreezeBalanceV2Processor();

    ForkController forkController = mock(ForkController.class);
    try (MockedStatic<ForkController> fork = Mockito.mockStatic(ForkController.class)) {
      fork.when(ForkController::instance).thenReturn(forkController);
      when(forkController.pass(VERSION_4_8_2_2)).thenReturn(false);
      processor.validate(param, repository);

      when(forkController.pass(VERSION_4_8_2_2)).thenReturn(true);
      assertFreezeV2Timeout(() -> processor.validate(param, repository));
    }
  }
```
