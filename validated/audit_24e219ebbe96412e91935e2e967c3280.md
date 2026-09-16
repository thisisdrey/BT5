## Title
TransferAssetActuator does not block TRC10 transfers to version-1 smart contracts, unlike TransferActuator - (File: `actuator/src/main/java/org/tron/core/actuator/TransferAssetActuator.java`)

### Summary
The reported bug class is a missing guard on one of two sibling transfer functions that exposes assets to being sent to a smart contract in a way the protocol does not intend to support. In java-tron, `TransferActuator` (plain TRX transfers) and `TransferAssetActuator` (TRC10 asset transfers) are structurally parallel actuators, both allowing `toAddress` to be a smart contract account. `TransferActuator.validate()` implements two protections against sending value to contract accounts, but `TransferAssetActuator.validate()` only implements one of them, omitting the version-1-contract check.

### Finding Description
`TransferActuator.validate()` contains two checks that guard against value being sent directly into contract accounts outside of `TriggerSmartContract`: [1](#0-0) 

The second check specifically forbids sending TRX to a smart contract whose `ContractVersion == 1` when `AllowTvmCompatibleEvm` is active, instructing the caller to use `TriggerSmartContract` instead.

`TransferAssetActuator.validate()`, which handles TRC10 (asset) transfers to arbitrary `toAddress` values, only re-implements the `ForbidTransferToContract` check and does **not** carry over the `AllowTvmCompatibleEvm`/`contractVersion == 1` guard: [2](#0-1) 

As a result, a `TransferAssetContract` (reachable directly by any signed transaction from an unprivileged account, no special permission required) can move TRC10 asset balances into a version-1 smart contract account even though the protocol explicitly considers that unsafe/unsupported for value transfers when `AllowTvmCompatibleEvm` is enabled, as evidenced by the parallel, deliberate block in `TransferActuator`.

### Impact Explanation
Version-1 contracts are treated specially under `AllowTvmCompatibleEvm` semantics (e.g. balance/energy handling differences, as seen in `Program.getCallEnergy` and `Program.setContractVersion`), and the TRX transfer path was explicitly hardened to reject direct sends to such contracts, directing users to `TriggerSmartContract` instead. Since `TransferAssetActuator` lacks the equivalent guard, TRC10 tokens sent via `TransferAssetContract` to a version-1 contract bypass the same protection the developers put in place for TRX, and can end up in an account that is not intended to receive assets this way, potentially becoming stuck/inaccessible through the intended `TriggerSmartContract` path. This is a fund-freezing risk analogous to the reported `depositERC20To` issue, reachable by any TRC10 holder issuing a normal `TransferAssetContract` transaction.

### Likelihood Explanation
Any account holding a TRC10 token can trigger this by sending a `TransferAssetContract` with `toAddress` set to a known version-1 smart contract address; no special privileges, admin approval, or contract cooperation is required — only that `AllowTvmCompatibleEvm` be enabled (a committee-approved, already-active chain parameter) and a version-1 contract exist on chain.

### Recommendation
Add the same `AllowTvmCompatibleEvm` / `contractVersion == 1` check present in `TransferActuator.validate()` (`actuator/src/main/java/org/tron/core/actuator/TransferActuator.java:141-156`) into `TransferAssetActuator.validate()`, rejecting TRC10 transfers to version-1 smart contracts and directing callers to use `TriggerSmartContract` instead, mirroring the existing `ForbidTransferToContract` check at lines 171-175 of `TransferAssetActuator.java`.

### Proof of Concept
1. Enable `AllowTvmCompatibleEvm` (already an active committee proposal parameter).
2. Deploy or identify an existing smart contract with `ContractVersion == 1`.
3. As any account holding a TRC10 asset, submit a `TransferAssetContract` transaction with `to_address` set to that contract's address and a positive `amount`.
4. `TransferAssetActuator.validate()` only checks `ForbidTransferToContract` (line 172) and does not check `contractVersion`, so validation succeeds and `execute()` moves the TRC10 balance into the version-1 contract account — the same operation that `TransferActuator` explicitly rejects for TRX under identical conditions (`TransferActuator.java:143-156`).

### Citations

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

**File:** actuator/src/main/java/org/tron/core/actuator/TransferAssetActuator.java (L169-192)
```java
    AccountCapsule toAccount = accountStore.get(toAddress);
    if (toAccount != null) {
      //after ForbidTransferToContract proposal, send trx to smartContract by actuator is not allowed.
      if (dynamicStore.getForbidTransferToContract() == 1
          && toAccount.getType() == AccountType.Contract) {
        throw new ContractValidateException("Cannot transfer asset to smartContract.");
      }

      assetBalance = toAccount.getAsset(dynamicStore, ByteArray.toStr(assetName));
      if (assetBalance != null) {
        try {
          assetBalance = addExact(assetBalance, amount); //check if overflow
        } catch (Exception e) {
          logger.debug(e.getMessage(), e);
          throw new ContractValidateException(e.getMessage());
        }
      }
    } else {
      fee = fee + dynamicStore.getCreateNewAccountFeeInSystemContract();
      if (ownerAccount.getBalance() < fee) {
        throw new ContractValidateException(
            "Validate TransferAssetActuator error, insufficient fee.");
      }
    }
```
