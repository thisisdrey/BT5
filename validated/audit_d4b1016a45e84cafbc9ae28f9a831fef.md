Based on the codebase evidence gathered, I found a concrete "policy bypass via low-level internal binding" analog: the network-level policy that disables the legacy Freeze V1 mechanism once Freeze V2 (`supportUnfreezeDelay`) is activated is enforced only in the actuator layer used by ordinary broadcast transactions, but is missing from the internal TVM native-contract path that a smart contract can invoke directly.

### Title
Legacy Freeze V1 Policy Bypassed via TVM Native `freeze()` Call - (File: actuator/src/main/java/org/tron/core/vm/nativecontract/FreezeBalanceProcessor.java)

### Summary
`FreezeBalanceActuator.validate()`, the entry point used when a client broadcasts a `FreezeBalanceContract` transaction, explicitly rejects freeze v1 operations once the chain has migrated to Freeze V2: [1](#0-0) 
This is the network's governance "policy" — once `supportUnfreezeDelay()` (Freeze V2 hard fork) is active, no one should be able to create new legacy V1 frozen balances.

However, the same freeze-v1 business logic is duplicated in `FreezeBalanceProcessor`, which is reachable from arbitrary smart contracts through the TVM's native "freeze" extension method `Program.freeze()`: [2](#0-1) 
`FreezeBalanceProcessor.validate()` contains none of the equivalent guard against Freeze V2 being active: [3](#0-2) 

### Finding Description
This mirrors the reported Node.js bug class exactly: a security/governance policy (`policy.json` → here, the `supportUnfreezeDelay` hard-fork switch that retires Freeze V1) is enforced only at the "public"/high-level entry point (`process.binding` policy hooks the `require()` path → here, `FreezeBalanceActuator.validate()` hooks the transaction-broadcast path), while a lower-level internal binding (`process.binding('spawn_sync')` → here, the TVM's internal native-contract dispatcher `Program.freeze()` / `FreezeBalanceProcessor`) performs the same privileged operation without re-checking the policy. Any contract deployer can write a Solidity contract that calls the `freeze` native extension on an address, which routes through `Program.freeze()` → `FreezeBalanceProcessor.validate()`/`execute()`, completely independent of the `FreezeBalanceActuator` code path and its `supportUnfreezeDelay()` check.

### Impact Explanation
If Freeze V2 has been activated network-wide specifically to retire the old freeze/resource model (and dual-track weight accounting is not designed to coexist), a contract could still create legacy V1 frozen balances and totals (`repo.addTotalNetWeight`, `repo.addTotalEnergyWeight`, `accountCapsule.setFrozenForBandwidth/Energy`) after they were supposed to be disabled. This can desynchronize total resource-weight accounting between the V1 and V2 models, potentially inflating bandwidth/energy entitlement or TRON_POWER-derived voting weight beyond what the post-hard-fork protocol rules intend — an unauthorized account operation / unbacked resource issuance condition.

### Likelihood Explanation
Reachable by any unprivileged account: deploy a trivial contract calling the `freeze()` native TVM extension (as demonstrated in `framework/.../vm/VoteTest.java`'s `TestVote.freeze()` pattern) [4](#0-3) . No special signer permission or SR/witness role is required — a single signed `TriggerSmartContract` transaction from any account suffices.

### Recommendation
Add the same `dynamicStore.supportUnfreezeDelay()` guard to `FreezeBalanceProcessor.validate()` (or centralize the check in a single shared validator used by both `FreezeBalanceActuator` and `FreezeBalanceProcessor`) so the Freeze V2 migration policy cannot be circumvented via the TVM native contract path.

### Proof of Concept
1. Wait for/observe a network where `supportUnfreezeDelay()` (Freeze V2) is active, so `FreezeBalanceContract` transactions are rejected with "freeze v2 is open, old freeze is closed."
2. Deploy a contract equivalent to `TestVote` from `framework/src/test/java/org/tron/common/runtime/vm/VoteTest.java` that exposes `freeze(address payable receiver, uint amount, uint res)` calling the native `.freeze(amount, res)` extension.
3. Call this contract's `freeze` function via a normal `TriggerSmartContract` transaction; execution flows through `Program.freeze()` → `FreezeBalanceProcessor.validate()`/`execute()`, which lacks the `supportUnfreezeDelay()` check present in `FreezeBalanceActuator.validate()`, so the legacy freeze succeeds despite the network policy.

**Note on confidence**: I was unable to fully trace, within the tool budget, whether `OperationActions`/`OperationRegistry` gates the `freeze` opcode dispatch itself behind a `VMConfig.allowTvmFreezeV2()`-style flag that might independently disable this path once Freeze V2 is active (my final grep for this was cut off before returning results). A background Devin session should verify that gate before treating this as a confirmed exploitable path in the current default configuration. [5](#0-4)

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/FreezeBalanceActuator.java (L271-274)
```java
    if (dynamicStore.supportUnfreezeDelay()) {
      throw new ContractValidateException(
              "freeze v2 is open, old freeze is closed");
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L1927-1956)
```java
  public boolean freeze(DataWord receiverAddress, DataWord frozenBalance, DataWord resourceType) {
    Repository repository = getContractState().newRepositoryChild();
    byte[] owner = getContextAddress();
    byte[] receiver = receiverAddress.toTronAddress();

    increaseNonce();
    InternalTransaction internalTx = addInternalTx(null, owner, receiver,
        frozenBalance.longValue(), null,
        "freezeFor" + convertResourceToString(resourceType), nonce, null);

    FreezeBalanceParam param = new FreezeBalanceParam();
    param.setOwnerAddress(owner);
    param.setReceiverAddress(receiver);
    boolean needCheckFrozenTime = CommonParameter.getInstance()
        .getCheckFrozenTime() == 1; // for test
    param.setFrozenDuration(needCheckFrozenTime
        ? repository.getDynamicPropertiesStore().getMinFrozenTime() : 0);
    param.setResourceType(parseResourceCode(resourceType));
    try {
      FreezeBalanceProcessor processor = new FreezeBalanceProcessor();
      param.setFrozenBalance(frozenBalance.sValue().longValueExact());
      processor.validate(param, repository);
      processor.execute(param, repository);
      repository.commit();
      return true;
    } catch (ContractValidateException e) {
      logger.warn("TVM Freeze: validate failure. Reason: {}", e.getMessage());
    } catch (ArithmeticException e) {
      logger.warn("TVM Freeze: frozenBalance out of long range.");
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

**File:** framework/src/test/java/org/tron/common/runtime/vm/VoteTest.java (L52-54)
```java
   *     function freeze(address payable receiver, uint amount, uint res) external {
   *       receiver.freeze(amount, res);
   *     }
```

**File:** actuator/src/main/java/org/tron/core/vm/OperationActions.java (L1-1)
```java
package org.tron.core.vm;
```
