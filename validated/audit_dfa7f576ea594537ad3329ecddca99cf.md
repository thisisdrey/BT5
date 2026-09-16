Confirmed: `VMUtils.validateForSmartContract` (used by all TVM internal transfer paths — `Program.callToAddress`, `MUtil.transfer`, `suicide`) does not check `getForbidTransferToContract()` / `getAllowTvmCompatibleEvm()` version-1 restrictions, while `TransferActuator.validate()` and `TransferAssetActuator.validate()` do enforce them. This is a direct analog of the reported bug class: a permission/business-rule check applied only at the top-level EOA transaction actuator, bypassable through internal TVM calls.

### Title
Top-level `ForbidTransferToContract` / TVM-version transfer restriction is not enforced on internal TVM transfers, allowing bypass via smart-contract calls - (File: actuator/src/main/java/org/tron/core/vm/VMUtils.java)

### Summary
`TransferActuator.validate()` and `TransferAssetActuator.validate()` enforce a governance-controlled restriction, `getForbidTransferToContract() == 1`, that forbids sending TRX/TRC10 directly to a smart-contract account, plus a related `AllowTvmCompatibleEvm` restriction that forbids sending TRX to version-1 contracts [1](#0-0) . These checks exist only in the actuator-level `validate()` methods invoked for externally-submitted `TransferContract`/`TransferAssetContract` transactions.

### Finding Description
The equivalent value-transfer validation used for internal TVM calls, `VMUtils.validateForSmartContract`, performs balance/address sanity checks but does not check `DynamicPropertiesStore.getForbidTransferToContract()` or `getAllowTvmCompatibleEvm()` at all [2](#0-1) . This function is called from `MUtil.transfer`, which underlies TVM `CALL`/value-transfer opcodes handled by `Program.callToAddress`, as well as `Program.suicide`/`suicide2` (`SELFDESTRUCT`) [3](#0-2) [4](#0-3) [5](#0-4) . Consequently, a deployed contract can transfer TRX to any other contract address, or to a version-1 (TVM-Solidity) contract, purely through internal calls, even when the network-wide `ForbidTransferToContract`/`AllowTvmCompatibleEvm` governance proposals are enabled to prevent exactly that outcome for TRX transfers. This mirrors the reported bug class: the restriction is applied only at the top-level, externally-submitted transaction path and is silently skippable through internal/contract-originated calls.

### Impact Explanation
This is a Medium-severity design gap, not a fund-theft bug: the restriction being bypassed is a governance/compatibility safeguard (introduced to stop TRX being "stuck" in contracts that cannot handle it, and to prevent unintended interaction with legacy version-1 contracts), not an authorization boundary. However, its bypass undermines the guarantee the on-chain proposal was meant to provide network-wide, since any contract-mediated transfer path circumvents it while direct EOA transfers are blocked, defeating the purpose of the proposal for a subset of actors (contract deployers) who can still route TRX to protected addresses.

### Likelihood Explanation
Trivially reachable by any unprivileged account: deploy or call any contract that performs a low-level `call{value: x}(...)`/`transfer`/`selfdestruct` targeting a contract address, or a version-1 contract, while `ForbidTransferToContract`/`AllowTvmCompatibleEvm` is enabled. No special privileges, precise timing, or SR/witness cooperation are needed.

### Recommendation
Add the same `getForbidTransferToContract()` and `getAllowTvmCompatibleEvm()` (contract-version) checks to `VMUtils.validateForSmartContract` (and/or centralize the logic shared between `TransferActuator`/`TransferAssetActuator` and the TVM internal-transfer path) so the restriction is enforced consistently regardless of whether the value transfer originates from an externally-submitted transaction or from TVM bytecode execution (`CALL`, `SELFDESTRUCT`, etc.).

### Proof of Concept
1. Enable the `ForbidTransferToContract` proposal (`DynamicPropertiesStore.getForbidTransferToContract() == 1`), as exercised in `TransferActuatorTest.transferToSmartContractAddress`, which shows a direct `TransferContract` to a contract address failing with "Cannot transfer TRX to a smartContract." [6](#0-5) .
2. Deploy contract A (any account, unprivileged) whose code performs `address(contractB).call{value: x}("")` or `selfdestruct(payable(contractB))`, targeting contract address B.
3. Send a `TriggerSmartContractContract` to invoke A's function with `value > 0`.
4. Observe that the internal transfer succeeds via `Program.callToAddress` → `VMUtils.validateForSmartContract`, which never checks `getForbidTransferToContract()`, moving TRX into contract B despite the proposal being enabled — the exact operation that a direct `TransferContract` to B would have rejected.

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

**File:** actuator/src/main/java/org/tron/core/vm/utils/MUtil.java (L18-26)
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
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L482-496)
```java
    } else {
      createAccountIfNotExist(getContractState(), obtainer);
      try {
        MUtil.transfer(getContractState(), owner, obtainer, balance);
        if (VMConfig.allowTvmTransferTrc10()) {
          MUtil.transferAllToken(getContractState(), owner, obtainer);
        }
      } catch (ContractValidateException e) {
        if (VMConfig.allowTvmConstantinople()) {
          throw new TransferException(
              "transfer all token or transfer all trx failed in suicide: %s", e.getMessage());
        }
        throw new BytecodeExecutionException("transfer failure");
      }
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L1091-1120)
```java
    } else if (!ArrayUtils.isEmpty(senderAddress) && !ArrayUtils.isEmpty(contextAddress)
        && senderAddress != contextAddress && endowment > 0) {
      createAccountIfNotExist(deposit, contextAddress);
      if (!isTokenTransfer) {
        try {
          VMUtils
              .validateForSmartContract(deposit, senderAddress, contextAddress, endowment);
        } catch (ContractValidateException e) {
          if (VMConfig.allowTvmConstantinople()) {
            refundEnergy(msg.getEnergy().longValue(), REFUND_ENERGY_FROM_MESSAGE_CALL);
            throw new TransferException("transfer trx failed: %s", e.getMessage());
          }
          throw new BytecodeExecutionException(VALIDATE_FOR_SMART_CONTRACT_FAILURE, e.getMessage());
        }
        deposit.addBalance(senderAddress, -endowment);
        contextBalance = deposit.addBalance(contextAddress, endowment);
      } else {
        try {
          VMUtils.validateForSmartContract(deposit, senderAddress, contextAddress,
              tokenId, endowment);
        } catch (ContractValidateException e) {
          if (VMConfig.allowTvmConstantinople()) {
            refundEnergy(msg.getEnergy().longValue(), REFUND_ENERGY_FROM_MESSAGE_CALL);
            throw new TransferException("transfer trc10 failed: %s", e.getMessage());
          }
          throw new BytecodeExecutionException(VALIDATE_FOR_SMART_CONTRACT_FAILURE, e.getMessage());
        }
        deposit.addTokenBalance(senderAddress, tokenId, -endowment);
        deposit.addTokenBalance(contextAddress, tokenId, endowment);
      }
```

**File:** framework/src/test/java/org/tron/core/actuator/TransferActuatorTest.java (L500-514)
```java
    TransferActuator actuator = new TransferActuator();
    actuator.setChainBaseManager(dbManager.getChainBaseManager())
        .setAny(getContract(1, contractAddress));
    TransactionResultCapsule ret = new TransactionResultCapsule();
    try {
      actuator.validate();
      actuator.execute(ret);
      Assert.assertEquals(ret.getInstance().getRet(), code.SUCESS);
      AccountCapsule owner =
          dbManager.getAccountStore().get(ByteArray.fromHexString(OWNER_ADDRESS));
      AccountCapsule toAccount =
          dbManager.getAccountStore().get(contractAddress);
    } catch (ContractValidateException e) {
      Assert.assertTrue(e.getMessage().contains("Cannot transfer"));
    }
```
