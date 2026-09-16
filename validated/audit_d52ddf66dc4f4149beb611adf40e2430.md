## Analog Found

### Title
Missing `AllowTvmCompatibleEvm` version-1 contract guard in `TransferAssetActuator` (present in sibling `TransferActuator`) - (File: `actuator/src/main/java/org/tron/core/actuator/TransferAssetActuator.java`)

### Summary
The reported bug class is a missing modifier/validation check in one function that is correctly present in a sibling function performing the analogous operation, allowing an operation that should be blocked for a class of "special" targets. In java-tron, `TransferActuator.validate()` (TRX transfer) enforces an `AllowTvmCompatibleEvm`-gated check that forbids sending value directly to a version-1 (fully EVM-compatible) smart contract, but the analogous `TransferAssetActuator.validate()` (TRC10 asset transfer) omits this check entirely, even though it duplicates the sibling `ForbidTransferToContract` check.

### Finding Description
`TransferActuator.validate()` contains two contract-recipient guards before allowing a TRX transfer to complete:
1. `ForbidTransferToContract` — blocks any transfer to a contract account.
2. `AllowTvmCompatibleEvm` — additionally blocks transfers to accounts whose `ContractCapsule.getContractVersion() == 1` (fully EVM-compatible contracts), instructing the caller to instead use `TriggerSmartContract`. [1](#0-0) 

`TransferAssetActuator.validate()`, which performs the structurally identical operation for TRC10 assets, only reimplements guard #1 and completely omits guard #2: [2](#0-1) 

Both actuators credit the recipient's balance/asset map directly on the `AccountStore` without ever invoking the target contract's code/fallback logic (no VM execution occurs in either `execute()` path): [3](#0-2) [4](#0-3) 

Version-1 contracts are the fully EVM-compatible contract type introduced by `AllowTvmCompatibleEvm`, and the codebase treats direct-transfer to them specially precisely because such contracts are expected to only receive value via an actual `CALL` (through `TriggerSmartContract`, which does invoke the fallback/receive logic), not via this direct-balance-adjustment actuator path. This distinction is reflected elsewhere in the VM (e.g., `getCallEnergy` special-cases version-1 contracts to follow strict EVM 63/64 energy-forwarding rules), confirming the intent that these contracts must be interacted with only via true contract calls.

### Impact Explanation
An unprivileged asset issuer/holder can send TRC10 tokens directly to a version-1 (EVM-compatible) contract via `TransferAssetContract`, bypassing the restriction that TRON deliberately enforces for TRX transfers to the same class of contract. Because the recipient contract's code is never executed, the contract has no opportunity to reject, forward, or account for the incoming asset. For any version-1 contract that tracks token balances purely through its own internal accounting/events (rather than querying on-chain TRC10 balances), TRC10 assets sent this way become invisible to the contract's business logic while still being recorded on-chain as owned by the contract address — this is a form of permanent freezing of funds, since the contract exposes no function path to recover assets it was never made aware of receiving. This mirrors the underlying bug class from the report: a validation guard that exists to prevent an unsafe cross-boundary operation on one function is missing on a sibling function performing the same class of value-transfer operation.

### Likelihood Explanation
Reachable directly by any account holding TRC10 tokens broadcasting a single signed `TransferAssetContract` transaction to a known version-1 contract address — no special privileges, no dependence on malicious operators, network conditions, or off-chain components. The condition only requires `AllowTvmCompatibleEvm` to be active (a live committee-enabled feature per `common/src/main/resources/reference.conf`) and a version-1 contract to exist as recipient.

### Recommendation
Add the equivalent `AllowTvmCompatibleEvm` / `ContractCapsule.getContractVersion() == 1` guard to `TransferAssetActuator.validate()`, matching the check already present in `TransferActuator.validate()`, so that TRC10 transfers to version-1 contracts are rejected the same way TRX transfers are, directing callers to use `TriggerSmartContract` instead.

### Proof of Concept
1. Enable `AllowTvmCompatibleEvm` (committee proposal #60).
2. Deploy a version-1 (EVM-compatible) contract that tracks TRC10 balances only via internal state updated inside its own functions (never re-queries on-chain asset balance).
3. Issue/hold a TRC10 asset as an ordinary account.
4. Broadcast a `TransferAssetContract` transaction sending the TRC10 asset directly to the contract's address.
5. Observe that `TransferAssetActuator.validate()` succeeds (no version-1 check), the contract's on-chain asset balance increases, but the contract's internal ledger never reflects the deposit — the asset is now permanently unrecoverable through the contract's exposed functions, whereas the same attempt using `TransferContract` (TRX) at the same code path would have been rejected with "Cannot transfer TRX to a smartContract which version is one."

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/TransferActuator.java (L60-66)
```java
      adjustBalance(accountStore, ownerAddress, -(addExact(fee, amount)));
      if (dynamicStore.supportBlackHoleOptimization()) {
        dynamicStore.burnTrx(fee);
      } else {
        adjustBalance(accountStore, accountStore.getBlackhole(), fee);
      }
      adjustBalance(accountStore, toAddress, amount);
```

**File:** actuator/src/main/java/org/tron/core/actuator/TransferActuator.java (L132-156)
```java
      //after ForbidTransferToContract proposal, send trx to smartContract by actuator is not allowed.
      if (dynamicStore.getForbidTransferToContract() == 1
          && toAccount != null
          && toAccount.getType() == AccountType.Contract) {

        throw new ContractValidateException("Cannot transfer TRX to a smartContract.");

      }

      // after AllowTvmCompatibleEvm proposal, send trx to smartContract which version is one
      // by actuator is not allowed.
      if (dynamicStore.getAllowTvmCompatibleEvm() == 1
          && toAccount != null
          && toAccount.getType() == AccountType.Contract) {

        ContractCapsule contractCapsule = chainBaseManager.getContractStore().get(toAddress);
        if (contractCapsule == null) { //  this can not happen
          throw new ContractValidateException(
              "Account type is Contract, but it is not exist in contract store.");
        } else if (contractCapsule.getContractVersion() == 1) {
          throw new ContractValidateException(
              "Cannot transfer TRX to a smartContract which version is one. "
                  + "Instead please use TriggerSmartContract ");
        }
      }
```

**File:** actuator/src/main/java/org/tron/core/actuator/TransferAssetActuator.java (L75-84)
```java
      AccountCapsule ownerAccountCapsule = accountStore.get(ownerAddress);
      if (!ownerAccountCapsule
          .reduceAssetAmountV2(assetName.toByteArray(), amount, dynamicStore, assetIssueStore)) {
        throw new ContractExeException("reduceAssetAmount failed !");
      }
      accountStore.put(ownerAddress, ownerAccountCapsule);

      toAccountCapsule
          .addAssetAmountV2(assetName.toByteArray(), amount, dynamicStore, assetIssueStore);
      accountStore.put(toAddress, toAccountCapsule);
```

**File:** actuator/src/main/java/org/tron/core/actuator/TransferAssetActuator.java (L169-175)
```java
    AccountCapsule toAccount = accountStore.get(toAddress);
    if (toAccount != null) {
      //after ForbidTransferToContract proposal, send trx to smartContract by actuator is not allowed.
      if (dynamicStore.getForbidTransferToContract() == 1
          && toAccount.getType() == AccountType.Contract) {
        throw new ContractValidateException("Cannot transfer asset to smartContract.");
      }
```
