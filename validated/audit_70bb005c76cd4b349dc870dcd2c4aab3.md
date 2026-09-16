This confirms the analog: `MUtil.transfer` and `MUtil.transferToken` — the routines that move TRX and TRC10 balances during a normal `CALL`/`transferToken` inside TVM execution — invoke `VMUtils.validateForSmartContract`, which does **not** enforce the `ForbidTransferToContract` restriction that `TransferActuator`/`TransferAssetActuator` enforce for regular (non-contract) broadcast transactions.

### Title
Inconsistent enforcement of `ForbidTransferToContract` restriction between direct-transfer actuators and TVM internal transfers - (File: `actuator/src/main/java/org/tron/core/vm/VMUtils.java`)

### Summary
`TransferActuator.validate()` and `TransferAssetActuator.validate()` explicitly block sending TRX or TRC10 tokens to a smart-contract account once the `ForbidTransferToContract` dynamic parameter is enabled by governance [1](#0-0) [2](#0-1) . However, the equivalent internal-transfer helpers used by the TVM (`MUtil.transfer` and `MUtil.transferToken`, called by `Program` during `CALL`/opcode execution) delegate to `VMUtils.validateForSmartContract`, which performs address/balance/overflow checks only and never checks `getForbidTransferToContract()` or the account type of the recipient [3](#0-2) [4](#0-3) .

### Finding Description
This mirrors the reported bug class: a restriction that is enforced in one code path (the ERC20 `_beforeTokenTransfer` hook / here, the actuator `validate()` methods) is silently skipped in another reachable path that performs the same underlying state mutation (ERC20's inherited `transfer`/`transferFrom` / here, TVM `CALL` value-transfer and `transferToken` opcode handling in `Program.callToPrecompiledAddress` and the `CALL` opcode logic that both funnel into `MUtil.transfer`/`MUtil.transferToken`). Any account can deploy a contract, or call an existing contract, whose bytecode performs a `CALL` with non-zero value, or executes the `transferToken` opcode, targeting another contract address — moving TRX or TRC10 balance into that contract even while `ForbidTransferToContract` is set to 1, bypassing the exact restriction that direct `TransferContract`/`TransferAssetContract` transactions are barred from doing.

### Impact Explanation
This is a governance-parameter bypass rather than a fund-theft bug: `ForbidTransferToContract` exists to prevent TRX/TRC10 from being stranded/mismanaged in smart contracts that may not handle them properly (particularly relevant historically for TVM-version-1 contracts that cannot process incoming value safely, as also checked separately in `TransferActuator` for `AllowTvmCompatibleEvm`) [5](#0-4) . Bypassing it can lead to value being sent to contracts that were never designed/audited to receive TRX/tokens, risking funds becoming permanently stuck (frozen) in a contract with no withdrawal path — a real-impact "permanent freezing of funds" outcome reachable by any unprivileged deployer/caller.

### Likelihood Explanation
High reachability: any address can deploy a trivial contract that does `address(other).call{value: x}("")` or uses `transferToken`, requiring only normal transaction broadcasting and TVM execution — no privileged role needed. The restriction is only meaningful once `ForbidTransferToContract` is enabled on the network, so likelihood depends on that governance state, but when enabled, the bypass is trivially and repeatably reachable by any caller.

### Recommendation
Add the same `ForbidTransferToContract` (and, for consistency, the `AllowTvmCompatibleEvm` version-1 contract) check inside `VMUtils.validateForSmartContract` (both TRX and TRC10 overloads), mirroring the logic already present in `TransferActuator.validate()` and `TransferAssetActuator.validate()`, so that TVM-internal transfers to contract accounts are subject to the same restriction as directly broadcast transfer transactions.

### Proof of Concept
Conceptual PoC (pending live verification, since this repo view is read-only):
1. Governance/committee enables `ForbidTransferToContract` (`DynamicPropertiesStore.getForbidTransferToContract() == 1`).
2. Deploy Contract A (attacker-controlled) and Contract B (any contract, does not need to opt-in to receiving value).
3. Call Contract A's function that executes `address(B).call{value: 1}("")` or the `transferToken` TVM opcode targeting B.
4. Observe that `MUtil.transfer`/`MUtil.transferToken` → `VMUtils.validateForSmartContract` succeeds and completes the balance transfer to contract B, even though a direct `TransferContract`/`TransferAssetContract` transaction to B would be rejected with `"Cannot transfer TRX to a smartContract."` / `"Cannot transfer asset to smartContract."` [1](#0-0) .

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/TransferActuator.java (L132-139)
```java
      //after ForbidTransferToContract proposal, send trx to smartContract by actuator is not allowed.
      if (dynamicStore.getForbidTransferToContract() == 1
          && toAccount != null
          && toAccount.getType() == AccountType.Contract) {

        throw new ContractValidateException("Cannot transfer TRX to a smartContract.");

      }
```

**File:** actuator/src/main/java/org/tron/core/actuator/TransferActuator.java (L141-156)
```java
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

**File:** actuator/src/main/java/org/tron/core/actuator/TransferAssetActuator.java (L171-175)
```java
      //after ForbidTransferToContract proposal, send trx to smartContract by actuator is not allowed.
      if (dynamicStore.getForbidTransferToContract() == 1
          && toAccount.getType() == AccountType.Contract) {
        throw new ContractValidateException("Cannot transfer asset to smartContract.");
      }
```

**File:** actuator/src/main/java/org/tron/core/vm/VMUtils.java (L137-180)
```java
  public static boolean validateForSmartContract(Repository deposit, byte[] ownerAddress,
      byte[] toAddress, long amount) throws ContractValidateException {
    if (!DecodeUtil.addressValid(ownerAddress)) {
      throw new ContractValidateException("Invalid ownerAddress!");
    }
    if (!DecodeUtil.addressValid(toAddress)) {
      throw new ContractValidateException("Invalid toAddress!");
    }

    if (Arrays.equals(toAddress, ownerAddress)) {
      throw new ContractValidateException("Cannot transfer TRX to yourself.");
    }

    AccountCapsule ownerAccount = deposit.getAccount(ownerAddress);
    if (ownerAccount == null) {
      throw new ContractValidateException("Validate InternalTransfer error, no OwnerAccount.");
    }

    AccountCapsule toAccount = deposit.getAccount(toAddress);
    if (toAccount == null) {
      throw new ContractValidateException(
          "Validate InternalTransfer error, no ToAccount. And not allowed to create an account in a smartContract.");
    }

    long balance = ownerAccount.getBalance();

    if (amount < 0) {
      throw new ContractValidateException("Amount must be greater than or equals 0.");
    }

    try {
      if (balance < amount) {
        throw new ContractValidateException(
            "Validate InternalTransfer error, balance is not sufficient.");
      }

      addExact(toAccount.getBalance(), amount, VMConfig.disableJavaLangMath());
    } catch (ArithmeticException e) {
      logger.debug(e.getMessage(), e);
      throw new ContractValidateException(e.getMessage());
    }

    return true;
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/utils/MUtil.java (L18-52)
```java
  public static void transfer(Repository deposit, byte[] fromAddress, byte[] toAddress, long amount)
      throws ContractValidateException {
    if (0 == amount) {
      return;
    }
    VMUtils.validateForSmartContract(deposit, fromAddress, toAddress, amount);
    deposit.addBalance(toAddress, amount);
    deposit.addBalance(fromAddress, -amount);
  }

  public static void transferAllToken(Repository deposit, byte[] fromAddress, byte[] toAddress) {
    AccountCapsule fromAccountCap = deposit.getAccount(fromAddress);
    Protocol.Account.Builder fromBuilder = fromAccountCap.getInstance().toBuilder();
    AccountCapsule toAccountCap = deposit.getAccount(toAddress);
    toAccountCap.importAllAsset();
    Protocol.Account.Builder toBuilder = toAccountCap.getInstance().toBuilder();
    fromAccountCap.getAssetMapV2().forEach((tokenId, amount) -> {
      toBuilder.putAssetV2(tokenId, toBuilder.getAssetV2Map().getOrDefault(tokenId, 0L) + amount);
      fromBuilder.putAssetV2(tokenId, 0L);
    });

    deposit.putAccountValue(fromAddress, new AccountCapsule(fromBuilder.build()));
    deposit.putAccountValue(toAddress, new AccountCapsule(toBuilder.build()));
  }

  public static void transferToken(Repository deposit, byte[] fromAddress, byte[] toAddress,
      String tokenId, long amount)
      throws ContractValidateException {
    if (0 == amount) {
      return;
    }
    VMUtils.validateForSmartContract(deposit, fromAddress, toAddress, tokenId.getBytes(), amount);
    deposit.addTokenBalance(toAddress, tokenId.getBytes(), amount);
    deposit.addTokenBalance(fromAddress, tokenId.getBytes(), -amount);
  }
```
