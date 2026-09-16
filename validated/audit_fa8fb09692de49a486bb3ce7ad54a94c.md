Found a direct analog to the Lens `FollowNFT` bug: a governance-gated feature check enforced in the ordinary transaction actuator but missing from the equivalent TVM-native-contract code path that a smart contract (callable by any unprivileged account) can reach via the `unDelegateResource` opcode/precompile.

### Title
Committee-gated `UNFREEZE_DELAY_DAYS` restriction on resource undelegation is bypassed via the TVM `unDelegateResource` opcode - (File: `actuator/src/main/java/org/tron/core/vm/nativecontract/UnDelegateResourceProcessor.java`)

### Summary
`UnDelegateResourceActuator.validate()` (the path taken by a normal, broadcast `UnDelegateResourceContract` transaction) requires that the committee has enabled `supportUnfreezeDelay()` (backed by the `UNFREEZE_DELAY_DAYS` chain parameter) before a user may unDelegate resources: [1](#0-0) 

However, `UnDelegateResourceProcessor.validate()` — the code invoked when a smart contract calls the TVM `unDelegateResource` instruction — never checks `supportUnfreezeDelay()`; it only checks `supportDR()`: [2](#0-1) 

This second path is reachable from `Program.unDelegateResource(...)`, which any deployed contract can invoke via the `unDelegateResource` TVM opcode: [3](#0-2) 

### Finding Description
This mirrors the Lens `FollowNFT` bug exactly: the "front door" (`unfollow()` / `UnDelegateResourceActuator`) enforces a committee/governance gate, while a "back door" (`FollowNFT.removeFollower()` / `Program.unDelegateResource` → `UnDelegateResourceProcessor`) that performs the same state mutation omits the gate. In java-tron, `ALLOW_TVM_FREEZE`'s prerequisite chain explicitly requires `ALLOW_DELEGATE_RESOURCE` to be enabled first (see `ProposalUtil.ALLOW_TVM_FREEZE` validator), and `supportUnfreezeDelay()` in `UnDelegateResourceActuator` is the governance switch meant to gate whether unDelegate is allowed on the network at all. The TVM-native processor used by the `unDelegateResource` opcode duplicates all the business logic of the actuator (balance checks, receiver checks, etc.) but drops this one governance check, meaning a contract call can unDelegate resources even when the committee has not turned on `supportUnfreezeDelay()` (or in any future scenario where committee wants to temporarily disable/re-gate this feature while leaving `supportDR()` on).

### Impact Explanation
Any deployed smart contract (reachable by any unprivileged account issuing a `TriggerSmartContract`) can perform resource undelegation logic that the protocol otherwise disallows through the standard actuator gate, exactly analogous to the Lens Medium-severity finding where an unprivileged user bypassed a governance pause via a secondary contract entry point. This breaks the invariant that `supportUnfreezeDelay()` fully controls whether unDelegate-type operations are permitted on-chain, impacting protocol functionality/availability control by governance — the same reasoning the C4 judge used to raise the original finding to Medium ("functions being available in moments they shouldn't be").

### Likelihood Explanation
Any account able to deploy a contract and call `unDelegateResource(...)` (a normal TVM instruction, no special permission required) can trigger this path; the bypass is deterministic and requires no race condition, only the network being in the specific state where `supportDR()` is enabled but `supportUnfreezeDelay()` is not (or any future case where committee disables one but not the other). Because the checks were clearly meant to be equivalent between actuator and native-contract implementations (as seen with the parallel `supportDR()` check present in both), the missing `supportUnfreezeDelay()` check is very likely an oversight rather than intentional design — but I could not find historical documentation confirming intent, so there's some uncertainty on whether the committee ever actually decouples these two flags in production configuration.

### Recommendation
Add the missing `if (!dynamicStore.supportUnfreezeDelay()) { throw new ContractValidateException(...); }` check to `UnDelegateResourceProcessor.validate()` in `actuator/src/main/java/org/tron/core/vm/nativecontract/UnDelegateResourceProcessor.java`, mirroring exactly what `UnDelegateResourceActuator.validate()` enforces, so the TVM opcode path cannot bypass the committee-controlled gate.

### Proof of Concept
1. Deploy any contract that calls the `unDelegateResource(uint256,uint256,address)` builtin (as used in `framework/src/test/java/org/tron/common/runtime/vm/FreezeV2Test.java`, e.g. `triggerUnDelegateResource(...)`). [4](#0-3) 
2. On a node/testnet where `supportDR()` returns true but `supportUnfreezeDelay()` returns false, attempt a normal `UnDelegateResourceContract` transaction via `UnDelegateResourceActuator` — it will fail validation with "Not support unDelegate resource transaction, need to be opened by the committee".
3. Call the same undelegation logic through the deployed contract's `unDelegateResource` TVM instruction instead — `UnDelegateResourceProcessor.validate()` only checks `supportDR()`, so the call succeeds and resources are undelegated despite the committee flag being off.

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

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/UnDelegateResourceProcessor.java (L32-50)
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
    if (ownerCapsule == null) {
      String readableOwnerAddress = StringUtil.createReadableString(ownerAddress);
      throw new ContractValidateException(
          ACCOUNT_EXCEPTION_STR + readableOwnerAddress + "] does not exist");
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

**File:** framework/src/test/java/org/tron/common/runtime/vm/FreezeV2Test.java (L282-290)
```java
  private TVMTestResult triggerUnDelegateResource(
      byte[] callerAddr, byte[] contractAddr, contractResult expectedResult,
      Consumer<byte[]> check, byte[] receiverAddr, long amount, long res)
      throws Exception {
    return triggerContract(
        callerAddr, contractAddr, fee, expectedResult, check,
        "unDelegateResource(uint256,uint256,address)",
        amount, res, StringUtil.encode58Check(receiverAddr));
  }
```
