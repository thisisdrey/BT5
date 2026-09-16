### Title
Old-style `freeze()` native contract invoked from a smart contract bypasses the "freeze v2 is open, old freeze is closed" guard, desynchronizing the v1/v2 resource models - ([File: actuator/src/main/java/org/tron/core/vm/nativecontract/FreezeBalanceProcessor.java])

### Summary
`FreezeBalanceActuator.validate()`, the entry point used for a normal `FreezeBalanceContract` transaction, explicitly forbids use of the legacy (v1) freeze mechanism once the network has switched to the new resource model: [1](#0-0) 

The equivalent native-contract path that is reachable from inside a TVM smart contract, `FreezeBalanceProcessor.validate()`, performs the amount/receiver checks but has no such guard, so it silently allows the legacy freeze to keep functioning even after `supportUnfreezeDelay()` (freeze v2) has been activated: [2](#0-1) 

This is the same class of bug as the reported issue: a critical protocol invariant ("only one freeze accounting model may be active at a time") is validated on one execution path but omitted on the other, letting the two subsystems (v1 legacy frozen-balance/weight accounting and v2 delayed-unfreeze accounting) drift out of sync — analogous to the L1/L2 bridge inflation-multiplier desync in the reference report.

### Finding Description
java-tron maintains two parallel resource-freezing models:
- Legacy model (`FreezeBalanceContract` / `UnfreezeBalanceContract`), which unfreezes immediately with no delay, tracked via `AccountCapsule.getFrozenBalance()` / `getEnergyFrozenBalance()`.
- New model ("freeze v2", gated by `DynamicPropertiesStore.supportUnfreezeDelay()`), which requires unfreezing to go through a delay/withdraw-expiration flow (`UnfreezeBalanceV2Actuator`, `WithdrawExpireUnfreezeActuator`).

The protocol's transaction-level `FreezeBalanceActuator` treats these models as mutually exclusive: once freeze v2 is turned on for the chain, it hard-rejects any attempt to use the old freeze contract: [1](#0-0) 

However, java-tron also exposes native-contract equivalents of these actuators to the TVM (invoked from `Program.java`, which references `FreezeBalanceProcessor`/`FreezeBalanceParam` in 10 places), allowing a deployed smart contract to freeze balance on behalf of itself or another address without going through `FreezeBalanceActuator` at all. `FreezeBalanceProcessor.validate()` re-implements the frozen-balance amount checks, the frozen-count check, the resource-type check, and the delegate-to-contract check — but it never checks `dynamicStore.supportUnfreezeDelay()`: [3](#0-2) 

As a result, after the community activates freeze v2 on-chain (intending to fully retire the old, non-delayed freeze/unfreeze mechanism), a smart contract can still call the native `freeze` functionality through the TVM and continue using the legacy accounting fields (`FrozenBalance`, `EnergyFrozenBalance`, `TotalNetWeight`/`TotalEnergyWeight` increments via the old code path in `FreezeBalanceProcessor.execute()`), while ordinary account-level transactions using `FreezeBalanceContract` are correctly blocked.

### Impact Explanation
This asymmetry lets contract-triggered freezing keep operating under the deprecated v1 semantics after governance has decided the chain should exclusively use v2. Concretely:
- It undermines the delayed-unfreeze design that freeze v2 was introduced to enforce (v1 freeze/unfreeze has no lock/withdraw-expiration period), letting resource providers churn stake without the intended delay — a direct economic/consensus-design bypass reachable by any unprivileged contract deployer.
- It keeps two divergent accounting paths (`Frozen`/`AccountResource.FrozenBalanceForEnergy` vs `frozen_v2` fields) simultaneously live on-chain when the protocol assumes only one is active post-hardfork, risking inconsistent `TotalNetWeight`/`TotalEnergyWeight` bookkeeping between the two models — the same "desync between two subsystems that must remain equal" root cause as the reported bridge bug.

### Likelihood Explanation
Reaching this path only requires an ordinary user to deploy or call a smart contract that invokes the native freeze functionality — no special privileges, no malicious SR/witness/peer behavior, and it becomes exploitable automatically as soon as freeze v2 is activated network-wide, which is the expected end-state of the chain.

### Recommendation
Add the same `dynamicStore.supportUnfreezeDelay()` rejection check to `FreezeBalanceProcessor.validate()` (and audit the sibling legacy `UnfreezeBalanceProcessor` native path for the same missing guard) so that the TVM-reachable native contract path enforces exactly the same "old freeze is closed once v2 is open" invariant as `FreezeBalanceActuator`.

### Proof of Concept
1. Wait for/observe the chain enabling freeze v2 (`supportUnfreezeDelay()` returns true), which normally makes all `FreezeBalanceContract` transactions fail with "freeze v2 is open, old freeze is closed" per `FreezeBalanceActuator.validate()`.
2. Deploy a smart contract (or use an existing one) that invokes the native `freeze` functionality mapped to `FreezeBalanceProcessor` from within the TVM.
3. Call that contract function with valid `frozenBalance`/`resourceType` parameters; because `FreezeBalanceProcessor.validate()` never checks `supportUnfreezeDelay()`, the call succeeds and updates the legacy frozen-balance fields and `TotalNetWeight`/`TotalEnergyWeight`, in contradiction to the protocol's stated post-v2 policy that is enforced only at the actuator layer.

Note: I was unable to fully trace the exact TVM opcode/precompile dispatch site in `Program.java` that wires `FreezeBalanceProcessor` into contract execution within the indexed portion of the codebase, so the precise call signature exposed to contracts could not be confirmed line-by-line; a full code review (e.g., via a Devin session with complete repo access) is recommended to pin down the exact invocation path and confirm there is no additional guard elsewhere in `Program.java` that compensates for this gap.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/FreezeBalanceActuator.java (L271-274)
```java
    if (dynamicStore.supportUnfreezeDelay()) {
      throw new ContractValidateException(
              "freeze v2 is open, old freeze is closed");
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
