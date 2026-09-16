### Title
TVM `unfreezeBalanceV2`/`freezeBalanceV2` opcodes bypass the committee-controlled `supportUnfreezeDelay` feature gate enforced by the normal transaction actuators - (File: `actuator/src/main/java/org/tron/core/vm/nativecontract/UnfreezeBalanceV2Processor.java`)

### Summary
`UnfreezeBalanceV2Actuator` (the normal `UnfreezeBalanceV2Contract` transaction path) refuses to execute unless the committee has enabled the "unfreeze delay"/new-resource-model feature via `dynamicStore.supportUnfreezeDelay()`. The TVM-reachable equivalent, `UnfreezeBalanceV2Processor` (invoked from `Program.unfreezeBalanceV2()` when a smart contract executes the `UNFREEZEBALANCEV2` opcode), never checks `supportUnfreezeDelay()`. The same pattern exists for `FreezeBalanceV2Processor` vs `FreezeBalanceV2Actuator`. This mirrors the Blend "flash loan bypasses frozen pool" bug class: a secondary execution path (contract opcode) reimplements the same state-mutating operation as a primary path (transaction actuator) but omits a governance/feature-status check that the primary path enforces.

### Finding Description
`UnfreezeBalanceV2Actuator.validate()` gates the operation behind the committee-controlled dynamic parameter: [1](#0-0) 

This check ensures that `UnfreezeBalanceV2Contract` transactions are rejected until the committee has opened (activated) the unfreeze-delay/new-resource-model feature.

However, the TVM-reachable `UnfreezeBalanceV2Processor.validate()`, which is invoked through the `UNFREEZEBALANCEV2` TVM opcode, performs address/account/frozen-balance checks but never checks `dynamicStore.supportUnfreezeDelay()`: [2](#0-1) 

The opcode itself is only gated by a separate, independent feature flag, `VMConfig::allowTvmFreezeV2`, at registration time: [3](#0-2) 

and is called from `Program.unfreezeBalanceV2()`, which only relies on the processor's `validate()`/`execute()`: [4](#0-3) 

`allowTvmFreezeV2` and `supportUnfreezeDelay` are two distinct committee-controlled proposals/dynamic parameters. Nothing in the codebase guarantees that they are always toggled in lockstep, so it is architecturally possible for the TVM-freeze-v2 opcode set to be enabled (`allowTvmFreezeV2 = true`) while the network-wide unfreeze-delay/new resource model feature is still disabled (`supportUnfreezeDelay = false`). In that state, ordinary transactions using `UnfreezeBalanceV2Contract` are correctly rejected, but a smart contract can still call the `UNFREEZEBALANCEV2` opcode and successfully mutate frozen/unfrozen V2 state, entirely bypassing the governance gate that the actuator enforces. The same divergence exists for freezing: `FreezeBalanceV2Actuator` checks `supportUnfreezeDelay`, but `FreezeBalanceV2Processor.validate()` does not perform this check either.

### Impact Explanation
The `supportUnfreezeDelay` flag controls activation of the new resource/freeze-V2 accounting model network-wide (delayed unfreeze, `FrozenV2`/`UnFreezeV2` lists, TRON power invalidation logic, total net/energy/tron-power weight bookkeeping). If this feature is not yet active but the TVM-freeze-v2 opcodes are, any contract can drive accounts into the FreezeV2/UnfreezeV2 accounting state (`addUnfrozenV2List`, `addFrozenBalanceForBandwidthV2/EnergyV2`, `addTotalNetWeight`/`addTotalEnergyWeight`) while the rest of the protocol (bandwidth/energy processors, vote/tron-power accounting, block-level assumptions) is still operating under the old-resource-model assumption that this state is not being created. This can desynchronize global resource-weight totals (`TotalNetWeight`/`TotalEnergyWeight`/`TotalTronPowerWeight`) and vote/tron-power accounting from what governance intended to permit, producing inconsistent resource accounting and unauthorized state transitions that the committee explicitly intended to withhold via the feature flag — an unauthorized account/protocol-state operation reachable by any contract caller.

### Likelihood Explanation
Reachable by any unprivileged account: deploy or call a contract that executes the Solidity built-in `unfreezeBalanceV2(...)`/`freezeBalanceV2(...)` syntax, which compiles to the `UNFREEZEBALANCEV2`/`FREEZEBALANCEV2` opcodes. The only precondition is that the committee has turned on `allowTvmFreezeV2` (a TVM feature flag) without necessarily having turned on `supportUnfreezeDelay` (a separate resource-model flag) — a state that is architecturally permitted since the two flags are independently maintained dynamic parameters, and no code paths were found asserting they must be enabled together.

### Recommendation
Add `if (!repo.getDynamicPropertiesStore().supportUnfreezeDelay()) { throw new ContractValidateException(...); }` to `FreezeBalanceV2Processor.validate()` and `UnfreezeBalanceV2Processor.validate()`, mirroring the check present in `FreezeBalanceV2Actuator`/`UnfreezeBalanceV2Actuator`, so the TVM opcode path enforces the same committee-controlled feature gate as the transaction path.

### Proof of Concept
1. Configure a test network where `VMConfig.allowTvmFreezeV2()` returns `true` (TVM freeze-v2 opcodes enabled) but `dynamicStore.supportUnfreezeDelay()` returns `false` (unfreeze-delay/new-resource-model not yet activated by committee).
2. Submit a normal `UnfreezeBalanceV2Contract` transaction: it is rejected by `UnfreezeBalanceV2Actuator.validate()` with `"Not support UnfreezeV2 transaction, need to be opened by the committee"` [1](#0-0) .
3. Deploy a contract exercising the `unfreezeBalanceV2(uint256,uint256)`/`freezeBalanceV2(uint256,uint256)` TVM opcode (as in `framework/src/test/java/org/tron/common/runtime/vm/VoteTest.java`'s freeze/unfreeze test harness) and invoke it via a normal `TriggerSmartContract` transaction.
4. Observe that `Program.unfreezeBalanceV2()`/`Program.freezeBalanceV2()` succeed via `UnfreezeBalanceV2Processor`/`FreezeBalanceV2Processor`, which do not check `supportUnfreezeDelay`, mutating `FrozenV2`/`UnFreezeV2` account state and global resource weights even though the feature is supposed to be disabled at the protocol level.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/UnfreezeBalanceV2Actuator.java (L119-122)
```java
    if (!dynamicStore.supportUnfreezeDelay()) {
      throw new ContractValidateException("Not support UnfreezeV2 transaction,"
          + " need to be opened by the committee");
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/UnfreezeBalanceV2Processor.java (L34-94)
```java
  public void validate(UnfreezeBalanceV2Param param, Repository repo)
      throws ContractValidateException {
    if (repo == null) {
      throw new ContractValidateException(STORE_NOT_EXIST);
    }

    byte[] ownerAddress = param.getOwnerAddress();
    DynamicPropertiesStore dynamicStore = repo.getDynamicPropertiesStore();
    if (!DecodeUtil.addressValid(ownerAddress)) {
      throw new ContractValidateException("Invalid address");
    }
    AccountCapsule accountCapsule = repo.getAccount(ownerAddress);
    if (accountCapsule == null) {
      String readableOwnerAddress = StringUtil.createReadableString(ownerAddress);
      throw new ContractValidateException(
          ACCOUNT_EXCEPTION_STR + readableOwnerAddress + "] does not exist");
    }
    long now = dynamicStore.getLatestBlockHeaderTimestamp();
    int unfreezingCount = accountCapsule.getUnfreezingV2Count(now);
    if (UnfreezeBalanceV2Actuator.getUNFREEZE_MAX_TIMES() <= unfreezingCount) {
      throw new ContractValidateException("Invalid unfreeze operation, unfreezing times is over limit");
    }
    switch (param.getResourceType()) {
      case BANDWIDTH:
        // validate frozen balance
        if (!this.checkExistFrozenBalance(accountCapsule, Common.ResourceCode.BANDWIDTH)) {
          throw new ContractValidateException("no frozenBalance(BANDWIDTH)");
        }
        break;
      case ENERGY:
        // validate frozen balance
        if (!this.checkExistFrozenBalance(accountCapsule, Common.ResourceCode.ENERGY)) {
          throw new ContractValidateException("no frozenBalance(ENERGY)");
        }
        break;
      case TRON_POWER:
        if (dynamicStore.supportAllowNewResourceModel()) {
          if (!this.checkExistFrozenBalance(accountCapsule, Common.ResourceCode.TRON_POWER)) {
            throw new ContractValidateException("no frozenBalance(TRON_POWER)");
          }
        } else {
          throw new ContractValidateException("Unknown ResourceCode, valid ResourceCode[BANDWIDTH、ENERGY]");
        }
        break;
      default:
        if (dynamicStore.supportAllowNewResourceModel()) {
          throw new ContractValidateException("Unknown ResourceCode, valid ResourceCode[BANDWIDTH、ENERGY、TRON_POWER]");
        } else {
          throw new ContractValidateException("Unknown ResourceCode, valid ResourceCode[BANDWIDTH、ENERGY]");
        }
    }

    if (!checkUnfreezeBalance(accountCapsule, param.getUnfreezeBalance(), param.getResourceType())) {
      throw new ContractValidateException(
          "Invalid unfreeze_balance, [" + param.getUnfreezeBalance() + "] is invalid");
    }

    if (accountCapsule.hasInvalidDelegatedV2()) {
      MUtil.checkCPUTimeForInvalidDelegatedV2Balance();
    }
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/OperationRegistry.java (L615-641)
```java
  public static void appendFreezeV2Operations(JumpTable table) {
    BooleanSupplier proposal = VMConfig::allowTvmFreezeV2;

    table.set(new Operation(
        Op.FREEZEBALANCEV2, 2, 1,
        EnergyCost::getFreezeBalanceV2Cost,
        OperationActions::freezeBalanceV2Action,
        proposal));

    table.set(new Operation(
        Op.UNFREEZEBALANCEV2, 2, 1,
        EnergyCost::getUnfreezeBalanceV2Cost,
        OperationActions::unfreezeBalanceV2Action,
        proposal));

    table.set(new Operation(
        Op.WITHDRAWEXPIREUNFREEZE, 0, 1,
        EnergyCost::getWithdrawExpireUnfreezeCost,
        OperationActions::withdrawExpireUnfreezeAction,
        proposal));

    table.set(new Operation(
        Op.CANCELALLUNFREEZEV2, 0, 1,
        EnergyCost::getCancelAllUnfreezeV2Cost,
        OperationActions::cancelAllUnfreezeV2Action,
        proposal));
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
