Based on my investigation, I found a concrete authorization-consistency gap in java-tron that mirrors the Gitea bug class: **two independent code paths perform the same privileged state-changing operation, but only one of them enforces a required governance/feature gate**.

### Title
Missing `supportUnfreezeDelay()` governance-gate check in TVM `DelegateResourceProcessor` allows contract-triggered resource delegation to bypass committee-controlled feature gating - (File: `actuator/src/main/java/org/tron/core/vm/nativecontract/DelegateResourceProcessor.java`)

### Summary
`DelegateResourceContract` (Stake 2.0 resource delegation) is reachable through two independent entry points that should enforce identical authorization/feature-gate rules: the normal broadcast-transaction path (`DelegateResourceActuator`) and the TVM native-contract path invoked from Solidity via the `delegateResource(uint256,uint256,address)` builtin (`Program.delegateResource`, which dispatches to `DelegateResourceProcessor`). The transaction path requires the committee-controlled `supportUnfreezeDelay()` flag to be enabled in addition to `supportDR()`, while the TVM native-contract path only checks `supportDR()`.

### Finding Description
`DelegateResourceActuator.validate()` gates the feature behind two dynamic-store flags: [1](#0-0) 

`DelegateResourceProcessor.validate()`, which is invoked from the TVM opcode handler `Program.delegateResource()` when a deployed contract calls the `delegateResource` native precompile, only checks `supportDR()` and omits the `supportUnfreezeDelay()` check entirely: [2](#0-1) 

The TVM entry point that reaches this processor is directly callable by any smart contract (i.e., by any account that deploys or calls a contract), with no additional authorization beyond normal contract execution: [3](#0-2) 

This is structurally the same bug class as the Gitea advisory: the "web"/canonical path (`DelegateResourceActuator`, analogous to Gitea's web fork handler) enforces the full authorization/feature-gate set, while the alternate "API"/native path (`DelegateResourceProcessor`, analogous to Gitea's API fork handler) omits one of the checks (`CanCreateOrgRepo` ↔ `supportUnfreezeDelay()`), letting an unprivileged caller reach privileged functionality that the committee has not yet turned on for the chain, purely by going through the second code path.

### Impact Explanation
`supportUnfreezeDelay()` is a chain-wide, committee-controlled proposal parameter that gates whether the Stake 2.0 "delegate/undelegate with lock period" feature set is active on the network. If a network operator/committee has enabled `AllowDelegateResource` (`supportDR`) but has intentionally not yet enabled `supportUnfreezeDelay` (e.g., during a phased rollout or because associated unlock/undelegate-timing logic is not yet considered safe), ordinary transaction senders are correctly blocked from calling `DelegateResourceContract`. However, any account able to deploy or call a smart contract can bypass this governance gate entirely and delegate/lock resources via the TVM native contract path, since `DelegateResourceProcessor.validate()` never checks `supportUnfreezeDelay()`. This breaks the security invariant that the flag uniformly controls whether the feature is live, and it can let stake/resource-delegation state be mutated on-chain in a way the committee explicitly intended to withhold — potentially in combination with other rollout-dependent invariants that assume the feature is fully inert until the flag flips.

### Likelihood Explanation
Exploitation requires nothing beyond the ability to deploy or call a smart contract (already assumed for any TRON account with minimal TRX for fees) and access to the `delegateResource` TVM builtin, which is unconditionally exposed once `supportDR()` is true — a condition independent of `supportUnfreezeDelay()`. No signature, permission, or off-chain component is needed; a single crafted contract call triggers the divergent path.

### Recommendation
Add the same `dynamicStore.supportUnfreezeDelay()` check to `DelegateResourceProcessor.validate()` (and audit the sibling `UnDelegateResourceProcessor`, `CancelAllUnfreezeV2Processor`, and other native-contract processors under `actuator/src/main/java/org/tron/core/vm/nativecontract/` for the same actuator/native-contract validation drift) so that every governance flag enforced in the corresponding `Actuator.validate()` is also enforced identically in the TVM native-contract `Processor.validate()` before allowing execution.

### Proof of Concept
1. As committee, enable `AllowDelegateResource` (`supportDR`) via proposal but leave `AllowTvmUnfreezeDelay`/`supportUnfreezeDelay` disabled.
2. As an unprivileged account, submit a normal `DelegateResourceContract` transaction — it is correctly rejected with `"Not support Delegate resource transaction, need to be opened by the committee"` by `DelegateResourceActuator.validate()`.
3. Deploy a trivial smart contract that freezes bandwidth/energy for itself (`FreezeBalanceV2`) and then invokes the `delegateResource(uint256,uint256,address)` TVM builtin targeting another account.
4. Observe that `Program.delegateResource()` → `DelegateResourceProcessor.validate()` succeeds and the delegation executes, despite `supportUnfreezeDelay()` being disabled — demonstrating the governance-gate bypass via the alternate (TVM) code path.

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

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/DelegateResourceProcessor.java (L33-55)
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
