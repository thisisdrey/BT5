### Title
`ForbidTransferToContract` and `AllowTvmCompatibleEvm` restrictions on sending TRX/TRC10 to smart contracts are enforced only in `TransferActuator`/`TransferAssetActuator`, not in the TVM's internal transfer path - ([File: actuator/src/main/java/org/tron/core/vm/utils/MUtil.java])

### Summary
The committee-controlled restriction that disallows sending TRX or TRC10 tokens directly to a smart-contract account (`ForbidTransferToContract`, plus the related `AllowTvmCompatibleEvm` version-1-contract restriction) is implemented as a check inside the top-level `TransferActuator.validate()` and `TransferAssetActuator.validate()` methods, but is completely absent from `VMUtils.validateForSmartContract()`, which is the function actually invoked by `MUtil.transfer()` / `MUtil.transferToken()` when TRX or TRC10 value moves during TVM execution (e.g. a Solidity `CALL` with value, `.transfer()`, `.send()`, or an internal TRC10 transfer opcode). A user can therefore bypass the restriction entirely by routing the transfer through a contract call instead of a direct `TransferContract`/`TransferAssetContract`.

### Finding Description
`TransferActuator.validate()` explicitly blocks sending TRX to a contract account when the flag is enabled: [1](#0-0) 

`TransferAssetActuator.validate()` implements the analogous restriction for TRC10 asset transfers: [2](#0-1) 

However, the actual value-movement routine used inside TVM execution, `MUtil.transfer()` / `MUtil.transferToken()`, delegates only to `VMUtils.validateForSmartContract()`: [3](#0-2) 

And `VMUtils.validateForSmartContract()` performs address validity, self-transfer, balance-sufficiency and overflow checks only — it never inspects `dynamicStore.getForbidTransferToContract()` or `dynamicStore.getAllowTvmCompatibleEvm()`, nor the recipient account's `AccountType`: [4](#0-3) 

This function is reached from `Program.java` during opcode execution (e.g. `CALL`/value transfer, and TRC10 internal transfer) whenever a smart contract sends TRX or a TRC10 token to another address, including another smart contract: [5](#0-4) 

Because this is the same architectural pattern reported externally — an access-control/business rule enforced only in a "wrapper" entry point (`TransferActuator`/`TransferAssetActuator`, analogous to `ArrakisV2Router`) while the underlying core execution path (`VMUtils`/TVM value-transfer, analogous to `ArrakisV2.mint()`) omits the check — any attacker can deploy or call a contract that internally issues a `CALL` with value (or an internal TRC10 transfer) to a smart-contract recipient, and the funds will move even though the committee explicitly disabled direct transfers to contracts via `ForbidTransferToContract`, or disabled sending TRX to legacy version-1 contracts via `AllowTvmCompatibleEvm`.

### Impact Explanation
`ForbidTransferToContract` and `AllowTvmCompatibleEvm` are committee-controlled chain parameters intended to prevent TRX/TRC10 from being trapped or mishandled by contracts that cannot properly process direct transfers (e.g., legacy version-1 contracts with unsafe fallback behavior, or contracts not designed to receive plain transfers). Bypassing this restriction via internal TVM calls can lead to funds being sent to contracts that are unable to handle or forward them correctly, undermining a deliberate safety guardrail set by chain governance. This does not directly create unbacked balances, but it defeats a protocol-level protection mechanism and can result in funds becoming stuck/mismanaged in contracts the committee specifically intended to shield from direct transfers.

### Likelihood Explanation
The bypass is trivially reachable: any account can deploy or invoke an already-deployed contract that performs a normal value-carrying `CALL` (Solidity `.call{value: x}()`, `.transfer()`, `.send()`) or a TRC10 token transfer to a target contract address. No special privileges, timing, or race conditions are required — the check is simply missing in the sole enforcement point.

### Recommendation
Move the `ForbidTransferToContract` (and `AllowTvmCompatibleEvm` version-1-contract) checks out of the actuators and into `VMUtils.validateForSmartContract()` (or a shared helper invoked by both the actuators and `MUtil.transfer`/`transferToken`), so that the restriction applies uniformly regardless of whether the transfer originates from a top-level `TransferContract`/`TransferAssetContract` or from an internal TVM value/token transfer during contract execution.

### Proof of Concept
1. Committee enables `ForbidTransferToContract` (dynamic property set to 1).
2. Attacker deploys `ContractA` with a function that does `payable(targetContractAddress).call{value: amount}("")` or an equivalent TRC10 transfer to `targetContractAddress` (a smart contract account).
3. Attacker calls `ContractA`'s function via `TriggerSmartContract`.
4. The value transfer succeeds because it routes through `MUtil.transfer()` → `VMUtils.validateForSmartContract()`, which never checks `ForbidTransferToContract`/`AllowTvmCompatibleEvm`, whereas an equivalent direct `TransferContract` to `targetContractAddress` would be rejected by `TransferActuator.validate()`.

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

**File:** actuator/src/main/java/org/tron/core/actuator/TransferAssetActuator.java (L169-176)
```java
    AccountCapsule toAccount = accountStore.get(toAddress);
    if (toAccount != null) {
      //after ForbidTransferToContract proposal, send trx to smartContract by actuator is not allowed.
      if (dynamicStore.getForbidTransferToContract() == 1
          && toAccount.getType() == AccountType.Contract) {
        throw new ContractValidateException("Cannot transfer asset to smartContract.");
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

**File:** actuator/src/main/java/org/tron/core/vm/VMUtils.java (L182-247)
```java
  public static boolean validateForSmartContract(Repository deposit, byte[] ownerAddress,
      byte[] toAddress, byte[] tokenId, long amount) throws ContractValidateException {
    if (deposit == null) {
      throw new ContractValidateException("No deposit!");
    }

    byte[] tokenIdWithoutLeadingZero = ByteUtil.stripLeadingZeroes(tokenId);

    if (!DecodeUtil.addressValid(ownerAddress)) {
      throw new ContractValidateException("Invalid ownerAddress");
    }
    if (!DecodeUtil.addressValid(toAddress)) {
      throw new ContractValidateException("Invalid toAddress");
    }

    if (amount <= 0) {
      throw new ContractValidateException("Amount must greater than 0.");
    }

    if (Arrays.equals(ownerAddress, toAddress)) {
      throw new ContractValidateException("Cannot transfer asset to yourself.");
    }

    AccountCapsule ownerAccount = deposit.getAccount(ownerAddress);
    if (ownerAccount == null) {
      throw new ContractValidateException("No owner account!");
    }

    if (deposit.getAssetIssue(tokenIdWithoutLeadingZero) == null) {
      throw new ContractValidateException("No asset !");
    }
    if (!Commons.getAssetIssueStoreFinal(deposit.getDynamicPropertiesStore(),
        deposit.getAssetIssueStore(), deposit.getAssetIssueV2Store())
        .has(tokenIdWithoutLeadingZero)) {
      throw new ContractValidateException("No asset !");
    }

    Long assetBalance = ownerAccount.getAsset(deposit.getDynamicPropertiesStore(),
            ByteArray.toStr(tokenIdWithoutLeadingZero));
    if (null == assetBalance || assetBalance <= 0) {
      throw new ContractValidateException("assetBalance must greater than 0.");
    }
    if (amount > assetBalance) {
      throw new ContractValidateException("assetBalance is not sufficient.");
    }

    AccountCapsule toAccount = deposit.getAccount(toAddress);
    if (toAccount != null) {
      assetBalance = toAccount.getAsset(deposit.getDynamicPropertiesStore(),
              ByteArray.toStr(tokenIdWithoutLeadingZero));
      if (assetBalance != null) {
        try {
          addExact(assetBalance, amount,
              VMConfig.disableJavaLangMath()); //check if overflow
        } catch (Exception e) {
          logger.debug(e.getMessage(), e);
          throw new ContractValidateException(e.getMessage());
        }
      }
    } else {
      throw new ContractValidateException(
          "Validate InternalTransfer error, no ToAccount. And not allowed to create account in smart contract.");
    }

    return true;
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L1712-1731)
```java
    // Charge for endowment - is not reversible by rollback
    if (!ArrayUtils.isEmpty(senderAddress) && !ArrayUtils.isEmpty(contextAddress)
        && senderAddress != contextAddress && msg.getEndowment().value().longValueExact() > 0) {
      if (!isTokenTransfer) {
        try {
          MUtil.transfer(deposit, senderAddress, contextAddress,
              msg.getEndowment().value().longValueExact());
        } catch (ContractValidateException e) {
          throw new BytecodeExecutionException("transfer failure");
        }
      } else {
        try {
          VMUtils
              .validateForSmartContract(deposit, senderAddress, contextAddress, tokenId, endowment);
        } catch (ContractValidateException e) {
          throw new BytecodeExecutionException(VALIDATE_FOR_SMART_CONTRACT_FAILURE, e.getMessage());
        }
        deposit.addTokenBalance(senderAddress, tokenId, -endowment);
        deposit.addTokenBalance(contextAddress, tokenId, endowment);
      }
```
