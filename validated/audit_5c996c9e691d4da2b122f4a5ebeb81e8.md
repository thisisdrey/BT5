### Title
`supportUnfreezeDelay` committee-gate check is missing in the TVM `unDelegateResource`/`delegateResource` native-contract path, letting any smart contract bypass the feature gate enforced on the regular transaction path - (File: `actuator/src/main/java/org/tron/core/vm/nativecontract/UnDelegateResourceProcessor.java`)

### Summary
This mirrors the reported Crestal bug class: the protocol enforces an authorization/feature gate in one entry point (`createAgentWithWhitelistUsers`) but exposes an alternate entry point (`createAgentWithNFT`) that performs the same privileged state change without that gate. In java-tron, `UnDelegateResourceActuator.validate()` (the regular-transaction path) requires both `dynamicStore.supportDR()` **and** `dynamicStore.supportUnfreezeDelay()` before allowing resource un-delegation [1](#0-0) . The alternate path reachable from inside a smart contract — `UnDelegateResourceProcessor.validate()`, invoked from the TVM opcode implementation in `Program.java` — only checks `supportDR()` and never checks `supportUnfreezeDelay()` [2](#0-1) . The sibling `DelegateResourceProcessor.validate()` (used by the TVM `delegateResource` opcode) has the same gap — it checks `supportDR()` but not `supportUnfreezeDelay()` — while `DelegateResourceActuator.validate()` checks both [3](#0-2) [4](#0-3) .

### Finding Description
`supportUnfreezeDelay()` is a committee-controlled proposal switch (`DynamicPropertiesStore`) that gates the "unfreeze delay / lock period" resource-delegation feature set, allowing the protocol to activate this behavior only once the community/committee approves it. The direct-transaction actuators (`DelegateResourceActuator`, `UnDelegateResourceActuator`) both explicitly enforce this gate before executing the state change [1](#0-0) .

However, the same resource delegation/un-delegation logic is also reachable from inside TVM contract execution through `Program.delegateResource(...)`, which constructs a `DelegateResourceParam` and calls `DelegateResourceProcessor.validate()`/`execute()` directly [5](#0-4) . This processor path — and the analogous `UnDelegateResourceProcessor` used for the un-delegate opcode — omits the `supportUnfreezeDelay()` check entirely, checking only `supportDR()` [6](#0-5) .

This is structurally identical to the Sherlock finding: two code paths that are supposed to perform the same privileged operation under the same precondition, but only one of them enforces the precondition. A smart contract caller (any unprivileged account deploying/calling a contract) can invoke the `delegateResource`/`unDelegateResource` TVM opcodes to exercise the feature even when the committee has not yet turned on `supportUnfreezeDelay`, bypassing the intended rollout/feature-flag control that governs when this functionality should be available on-chain.

### Impact Explanation
If the committee has enabled `supportDR` (basic resource delegation) but has intentionally not yet enabled `supportUnfreezeDelay` (e.g., during a phased rollout or because of unresolved edge cases in the lock-period logic), the direct transaction path correctly rejects delegate/un-delegate calls. But any contract can still reach the same state-changing logic via the TVM precompiled opcode path, executing delegate/un-delegate resource operations that the protocol has not authorized yet. Since `supportUnfreezeDelay` was introduced specifically to gate lock-period-related resource accounting changes, bypassing it risks inconsistent resource/stake accounting state (frozen/delegated balances, lock periods) before the feature is fully vetted — a governance/feature-gate bypass with potential for incorrect bandwidth/energy resource bookkeeping.

### Likelihood Explanation
Reachability requires only that `supportDR` is on while `supportUnfreezeDelay` is off — a realistic intermediate state during a phased committee rollout — and any account can deploy a trivial contract that calls the `delegateResource`/`unDelegateResource` TVM opcode to trigger the vulnerable code path with no special privilege. No malicious-SR/witness/committee assumption is needed; it is exploitable by a normal contract deployer/caller.

### Recommendation
Add the same `dynamicStore.supportUnfreezeDelay()` check to `DelegateResourceProcessor.validate()` and `UnDelegateResourceProcessor.validate()` (the TVM native-contract paths) that already exists in `DelegateResourceActuator.validate()` and `UnDelegateResourceActuator.validate()`, so both entry points enforce identical committee-gated preconditions.

### Proof of Concept
Not executed (index-only analysis); the analog is demonstrated by the divergent validation logic cited above: `UnDelegateResourceActuator.validate()` checks `supportUnfreezeDelay()` while `UnDelegateResourceProcessor.validate()` (reached via the TVM opcode) does not, and likewise for `DelegateResourceActuator` vs `DelegateResourceProcessor`.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/UnDelegateResourceActuator.java (L205-212)
```java
    if (!dynamicStore.supportDR()) {
      throw new ContractValidateException("No support for resource delegate");
    }

    if (!dynamicStore.supportUnfreezeDelay()) {
      throw new ContractValidateException("Not support unDelegate resource transaction,"
          + " need to be opened by the committee");
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/UnDelegateResourceProcessor.java (L32-45)
```java
  public void validate(UnDelegateResourceParam param, Repository repo) throws ContractValidateException {
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
```

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/DelegateResourceProcessor.java (L33-42)
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
```

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

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L2168-2189)
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
```
