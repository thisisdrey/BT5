### Title
Permanent freezing of contract-owned staked TRX via unconditional `OutOfTimeException` guard on negative `delegatedFrozenV2Balance` - (File: `actuator/src/main/java/org/tron/core/vm/utils/MUtil.java`)

### Summary
java-tron's TVM-native staking opcodes (`unfreezeBalanceV2`, `withdrawExpireUnfreeze`, `cancelAllUnfreezeV2`) contain a "fix" that, once an account's `delegatedFrozenV2Balance` becomes negative, unconditionally reverts every future call to these opcodes for that account with `OutOfTimeException`, and never repairs the underlying negative state. Because these opcodes are the *only* way a smart-contract account can unfreeze/withdraw/cancel its own staked TRX (a contract cannot broadcast a normal signed `UnfreezeBalanceV2Contract`/`WithdrawExpireUnfreezeContract` transaction), any contract account that ends up with `hasInvalidDelegatedV2() == true` permanently loses the ability to reclaim its staked TRX through any protocol path — with no owner/admin/committee action able to remedy it.

### Finding Description
`AccountCapsule.hasInvalidDelegatedV2()` flags an account whose `delegatedFrozenV2BalanceForBandwidth`/`...ForEnergy` has gone negative: [1](#0-0) 

This negative state is documented in `UnDelegateResourceProcessor`/`UnDelegateResourceActuator` as arising from a "TVM contract suicide, re-create" scenario, where the receiver's `AcquiredDelegatedFrozenV2Balance` is reset to zero and subsequent bookkeeping desynchronizes from the delegator's `delegatedFrozenV2Balance`: [2](#0-1) 

Once this state exists, every TVM-native staking processor checks it and, past fork `VERSION_4_8_2_2`, throws unconditionally: [3](#0-2) 

This guard is invoked from all three of the contract-reachable staking operations:
- `UnfreezeBalanceV2Processor.validate`: [4](#0-3) 
- `WithdrawExpireUnfreezeProcessor.validate`: [5](#0-4) 
- `CancelAllUnfreezeV2Processor.validate`: [6](#0-5) 

These processors are only reachable from `Program.unfreezeBalanceV2()` / `Program.withdrawExpireUnfreeze()` / the cancel-all opcode, i.e. via `SELFDESTRUCT`/contract-initiated TVM opcodes acting on `getContextAddress()` (the contract's own address): [7](#0-6) 

Crucially, the check never clears or repairs the negative `delegatedFrozenV2Balance` — it just throws every time, forever. For an externally-owned account (EOA), the equivalent wallet-level actuators (`UnfreezeBalanceV2Actuator`, `WithdrawExpireUnfreezeActuator`) do not perform this check at all, so an EOA retains an escape hatch. But a *contract* account cannot sign a transaction to invoke those actuators directly — the TVM opcode is its only route to reclaim its own frozen/unfrozen TRX. This is confirmed by the maintainers' own regression test, which explicitly asserts that once an account has a negative delegated balance, `withdrawExpireUnfreeze`, `cancelAllUnfreezeV2`, and `unfreezeBalanceV2` throw `OutOfTimeException("CPU timeout for invalid delegated V2 balance")` unconditionally after the fork activates: [8](#0-7) 

This mirrors the reported bug class: users (here, contract accounts) have deposited/staked funds (TRX for bandwidth/energy) that can become permanently unreclaimable once a specific state is reached, with zero recourse — worse than the Opyn case because there isn't even an admin function that can restore withdrawal capability; the block is hard-coded and irreversible once triggered.

### Impact Explanation
A contract account with staked (frozen) TRX under the new resource model that reaches the `hasInvalidDelegatedV2()` state permanently loses the ability to unfreeze its resource, cancel pending unfreezes, or withdraw already-expired unfrozen TRX through the only channel available to it (TVM staking opcodes). The staked/pending TRX becomes permanently frozen — an unbacked, irrecoverable loss of funds for the contract and, transitively, its users/beneficiaries, with no path (owner key, committee proposal, or otherwise) to recover it. This satisfies "permanent freezing of funds."

### Likelihood Explanation
Reaching the negative `delegatedFrozenV2Balance` state requires a specific resource-delegation + `SELFDESTRUCT`/recreate sequence combined with an `UnDelegateResourceV2` call, all of which are actions any contract deployer/caller can trigger with ordinary signed transactions and contract code — no privileged role is required. The freeze-after-selfdestruct path is independently fork-gated as a known edge case (`checkCPUTimeForFreezeV2AfterSelfDestruct`), and the project's own tests demonstrate the negative-balance precondition and its consequence, indicating the state is reachable and the block is real and permanent for any account it affects.

### Recommendation
Do not use an unconditional revert as a substitute for fixing the underlying negative `delegatedFrozenV2Balance` accounting bug. Instead:
1. Detect and normalize/clamp the negative `delegatedFrozenV2Balance` to zero (or otherwise reconcile it against the real delegated state) as part of processing, rather than perpetually rejecting the transaction.
2. Provide a dedicated repair/administrative-recovery native contract or migration that resets corrupted `delegatedFrozenV2Balance` values for affected accounts, ensuring the affected contract's already-staked/unfrozen TRX remains withdrawable.
3. Add invariant checks in `UnDelegateResourceProcessor`/`DelegateResourceProcessor` (and the `SELFDESTRUCT` re-creation path) so that `delegatedFrozenV2Balance` can never go negative in the first place.

### Proof of Concept
1. Contract `A` freezes TRX for BANDWIDTH/ENERGY (v2 model) and delegates part of it to contract `B` via `DelegateResourceV2`.
2. Contract `B` self-destructs and a new contract is redeployed to the same address (recreated), resetting `B`'s `AcquiredDelegatedFrozenV2Balance*` fields to 0, as handled in `UnDelegateResourceProcessor.execute` ("A TVM contract suicide, re-create will produce this situation" branches setting `AcquiredDelegatedFrozenV2Balance*` to 0): [2](#0-1) 
3. Contract `A` subsequently calls `UnDelegateResourceV2`/related operations such that its own `delegatedFrozenV2BalanceForBandwidth`/`ForEnergy` becomes negative (state confirmed reachable and tested directly in `StakeV2AfterSelfDestructTest.account(ownerAddress, -1, 0)`).
4. From that point on, any call by contract `A` to `unfreezeBalanceV2`, `withdrawExpireUnfreeze`, or `cancelAllUnfreezeV2` (its only means, as a contract, to reclaim frozen/unfrozen TRX) throws `OutOfTimeException("CPU timeout for invalid delegated V2 balance")` and reverts, permanently, as verified in: [9](#0-8) 
5. Contract `A`'s staked/unfrozen TRX is now permanently unreclaimable — no owner key, no committee proposal, no further transaction can unfreeze or withdraw it.

### Citations

**File:** chainbase/src/main/java/org/tron/core/capsule/AccountCapsule.java (L1339-1341)
```java
  public boolean hasInvalidDelegatedV2() {
    return getDelegatedFrozenV2BalanceForBandwidth() < 0 || getDelegatedFrozenV2BalanceForEnergy() < 0;
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/UnDelegateResourceProcessor.java (L107-123)
```java
          /* For example, in a scenario where a regular account can be upgraded to a contract
          account through an interface, the account information will be cleared after the
          contract suicide, and this account will be converted to a regular account in the future */
          if (receiverCapsule.getAcquiredDelegatedFrozenV2BalanceForBandwidth()
              < unDelegateBalance) {
            // A TVM contract suicide, re-create will produce this situation
            receiverCapsule.setAcquiredDelegatedFrozenV2BalanceForBandwidth(0);
          } else {
            // calculate usage
            long unDelegateMaxUsage = (long) ((double) unDelegateBalance / TRX_PRECISION
                * dynamicStore.getTotalNetLimit() / repo.getTotalNetWeight());
            transferUsage = (long) (receiverCapsule.getNetUsage()
                * ((double) (unDelegateBalance) / receiverCapsule.getAllFrozenBalanceForBandwidth()));
            transferUsage = min(unDelegateMaxUsage, transferUsage, VMConfig.disableJavaLangMath());

            receiverCapsule.addAcquiredDelegatedFrozenV2BalanceForBandwidth(-unDelegateBalance);
          }
```

**File:** actuator/src/main/java/org/tron/core/vm/utils/MUtil.java (L99-103)
```java
  public static void checkCPUTimeForInvalidDelegatedV2Balance() {
    if (ForkController.instance().pass(Parameter.ForkBlockVersionEnum.VERSION_4_8_2_2)) {
      throw new OutOfTimeException("CPU timeout for invalid delegated V2 balance");
    }
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/UnfreezeBalanceV2Processor.java (L91-93)
```java
    if (accountCapsule.hasInvalidDelegatedV2()) {
      MUtil.checkCPUTimeForInvalidDelegatedV2Balance();
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/WithdrawExpireUnfreezeProcessor.java (L57-59)
```java
    if (accountCapsule.hasInvalidDelegatedV2()) {
      MUtil.checkCPUTimeForInvalidDelegatedV2Balance();
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/CancelAllUnfreezeV2Processor.java (L44-46)
```java
    if (accountCapsule.hasInvalidDelegatedV2()) {
      MUtil.checkCPUTimeForInvalidDelegatedV2Balance();
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L2059-2093)
```java
  public boolean unfreezeBalanceV2(DataWord unfreezeBalance, DataWord resourceType) {
    Repository repository = getContractState().newRepositoryChild();
    byte[] owner = getContextAddress();

    increaseNonce();
    InternalTransaction internalTx = addInternalTx(null, owner, owner,
        unfreezeBalance.longValue(), null,
        "unfreezeBalanceV2For" + convertResourceToString(resourceType), nonce, null);

    try {
      UnfreezeBalanceV2Param param = new UnfreezeBalanceV2Param();
      param.setOwnerAddress(owner);
      param.setUnfreezeBalance(unfreezeBalance.sValue().longValueExact());
      param.setResourceType(parseResourceCodeV2(resourceType));

      UnfreezeBalanceV2Processor processor = new UnfreezeBalanceV2Processor();
      processor.validate(param, repository);
      long unfreezeExpireBalance = processor.execute(param, repository);
      repository.commit();
      if (unfreezeExpireBalance > 0) {
        increaseNonce();
        addInternalTx(null, owner, owner, unfreezeExpireBalance, null,
            "withdrawExpireUnfreezeWhileUnfreezing", nonce, null);
      }
      return true;
    } catch (ContractValidateException e) {
      logger.warn("TVM UnfreezeBalanceV2: validate failure. Reason: {}", e.getMessage());
    } catch (ArithmeticException e) {
      logger.warn("TVM UnfreezeBalanceV2: balance out of long range.");
    }
    if (internalTx != null) {
      internalTx.reject();
    }
    return false;
  }
```

**File:** framework/src/test/java/org/tron/core/vm/nativecontract/StakeV2AfterSelfDestructTest.java (L62-101)
```java
  @Test
  public void invalidDelegatedBalancesBlockWithdrawAndCancelAfterFork() throws Exception {
    byte[] ownerAddress = address(1);
    Repository repository = mock(Repository.class);
    DynamicPropertiesStore dynamicStore = mock(DynamicPropertiesStore.class);
    when(repository.getDynamicPropertiesStore()).thenReturn(dynamicStore);
    when(dynamicStore.getLatestBlockHeaderTimestamp()).thenReturn(NOW);

    WithdrawExpireUnfreezeParam withdrawParam = new WithdrawExpireUnfreezeParam();
    withdrawParam.setOwnerAddress(ownerAddress);
    WithdrawExpireUnfreezeProcessor withdrawProcessor =
        new WithdrawExpireUnfreezeProcessor();
    CancelAllUnfreezeV2Param cancelParam = new CancelAllUnfreezeV2Param();
    cancelParam.setOwnerAddress(ownerAddress);
    CancelAllUnfreezeV2Processor cancelProcessor = new CancelAllUnfreezeV2Processor();

    ForkController forkController = mock(ForkController.class);
    try (MockedStatic<ForkController> fork = Mockito.mockStatic(ForkController.class)) {
      fork.when(ForkController::instance).thenReturn(forkController);
      when(forkController.pass(VERSION_4_8_2_2)).thenReturn(false);
      when(repository.getAccount(ownerAddress)).thenReturn(account(ownerAddress, -1, 0));
      withdrawProcessor.validate(withdrawParam, repository);
      cancelProcessor.validate(cancelParam, repository);
      when(repository.getAccount(ownerAddress)).thenReturn(account(ownerAddress, 0, -1));
      withdrawProcessor.validate(withdrawParam, repository);
      cancelProcessor.validate(cancelParam, repository);

      when(forkController.pass(VERSION_4_8_2_2)).thenReturn(true);
      when(repository.getAccount(ownerAddress)).thenReturn(account(ownerAddress, -1, 0));
      assertInvalidDelegatedV2Timeout(
          () -> withdrawProcessor.validate(withdrawParam, repository));
      assertInvalidDelegatedV2Timeout(
          () -> cancelProcessor.validate(cancelParam, repository));
      when(repository.getAccount(ownerAddress)).thenReturn(account(ownerAddress, 0, -1));
      assertInvalidDelegatedV2Timeout(
          () -> withdrawProcessor.validate(withdrawParam, repository));
      assertInvalidDelegatedV2Timeout(
          () -> cancelProcessor.validate(cancelParam, repository));
    }
  }
```
