### Title
Incomplete Cleanup of Self-Destructed Contract State Allows Storage/Nonce Inheritance on Address Reuse - (File: actuator/src/main/java/org/tron/core/vm/repository/RepositoryImpl.java)

### Summary
When a TVM contract self-destructs, `RepositoryImpl.deleteContract` only removes the account, code, and contract-capsule entries, but does not clear the contract's persistent storage rows or its `ContractStateCapsule` (nonce/self-destruct bookkeeping). If a new contract is later created at the same address (e.g. via `CREATE2` with the same salt/init-code, which is explicitly supported and tested in this codebase), the freshly deployed contract can read back stale `SLOAD` values and inherit residual bookkeeping left over from the destroyed contract — the same class of bug described in the Palmera report, where deleting an entity left residual state (`allowFeature`, `listed[org]`, `listCount`) that was later inherited by a new entity created with a reused identifier.

### Finding Description
`RepositoryImpl.deleteContract` is the code path invoked when a contract self-destructs (`Program.suicide`/`suicide2` → `getResult().addDeleteAccount(...)` → cleanup loop in `VMActuator`): [1](#0-0) 

This method deletes only three stores: `CodeStore`, `AccountStore`, `ContractStore`. It does **not**:
- delete the contract's `Storage` rows from `StorageRowStore` (the key-value state variables of the contract), and
- delete/reset the `ContractStateCapsule` tracked via `getContractState`/`updateContractState` (used for nonce and self-destruct state).

The `Storage` class computes row keys deterministically from the contract address (`Storage.compose`/`addrHash`), so any values written by the destroyed contract remain in `StorageRowStore` under that same address-derived key: [2](#0-1) 

`suicide`/`suicide2` mark the account for deletion but never touch its storage or `ContractStateCapsule`: [3](#0-2) [4](#0-3) 

Java-tron explicitly supports and tests deploying a new contract at a previously self-destructed address via `CREATE2` (deterministic address reuse), as shown by the `FreezeTest`/`Create2Test` test suites that predict an address, freeze/suicide a contract there, and then redeploy a contract to the very same predicted address: [5](#0-4) 

When `createContractImpl` deploys a new contract at an address that already has an account, it checks `isContractExist` only to decide contract-capsule/account-type handling, but the underlying `Storage`/`StorageRowStore` for that address is never cleared before the new contract begins executing its constructor: [6](#0-5) 

This mirrors the reported bug class exactly: `removeOrg`-equivalent cleanup (`deleteContract`) removes only some of the state associated with the entity (account/code/contract capsule) while leaving other state (storage slots, contract-state bookkeeping) behind, so a newly created entity at the reused identifier (address) can inherit unwanted residual state.

### Impact Explanation
If a contract's storage is not fully wiped before a new contract occupies the same address, the new contract's `SLOAD`s can return values it never wrote, silently corrupting its own invariants (e.g., access-control flags, balances/allowances encoded in storage, initialization guards). Depending on the redeployed contract's logic, this can lead to unauthorized privilege (e.g., an `initialized`/`owner` slot pre-populated from the old contract bypassing constructor logic), or inconsistent accounting that a caller could exploit — a concrete state-integrity violation reachable purely through ordinary `TriggerSmartContract`/`CreateSmartContract` transactions from any user, with no special/malicious-node privilege required.

### Likelihood Explanation
Exploitation requires: (1) a contract that self-destructs to a fixed address (deployable via `CREATE2` with attacker-chosen salt and init-code, entirely within a normal user's control), and (2) redeployment of a different/same contract to that address relying on “clean” initial storage. Because `CREATE2` address prediction and self-destruct-then-redeploy are both first-class, tested TVM features in this codebase (see `Create2Test`, `FreezeTest`), an unprivileged contract deployer fully controls all preconditions for this scenario, making it directly reachable, though the practical severity depends on whether a given contract's logic relies on storage slots being zero at construction.

### Recommendation
In `RepositoryImpl.deleteContract`, in addition to removing the code, account and contract entries, clear all storage rows associated with the address (iterate/delete the `StorageRowStore` entries under that address's `addrHash` prefix, or explicitly zero them via the existing `Storage.commit()`/delete path) and reset any associated `ContractStateCapsule` so a contract later created at the same address starts from fully zeroed state, consistent with the Yellow Paper's expectation that a self-destructed account's storage is cleared.

### Proof of Concept
1. Deploy `Factory` and predict a `CREATE2` address `predictedAddr` for salt `S` and init-code `C` (as in `Create2Test`/`FreezeTest.testCreate2SuicideToBlackHole`).
2. Deploy contract `A` to `predictedAddr` (via `CREATE2`) whose constructor writes a non-zero value to storage slot `k` (e.g., sets an `initialized` flag or balance).
3. Trigger `A.selfdestruct(...)`; observe that `AccountStore`, `CodeStore`, `ContractStore` entries for `predictedAddr` are removed via `RepositoryImpl.deleteContract`, but the row for slot `k` remains in `StorageRowStore` (no code path clears it).
4. Deploy a new contract `B` to the same `predictedAddr` via `CREATE2` with the same salt but different init-code whose constructor assumes slot `k` starts at zero (e.g., an `initialized`/`owner` guard).
5. Call `B`'s `SLOAD` on slot `k` (or a function reading it) and observe it returns the value left behind by `A`, demonstrating inherited residual state — analogous to a new "xyz" organization inheriting the deleted organization's `allowFeature`/`listed`/`listCount` state in the referenced report.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/repository/RepositoryImpl.java (L487-492)
```java
  @Override
  public void deleteContract(byte[] address) {
    getCodeStore().delete(address);
    getAccountStore().delete(address);
    getContractStore().delete(address);
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Storage.java (L46-84)
```java
  private byte[] compose(byte[] key, byte[] addrHash) {
    if (contractVersion == 1) {
      key = Hash.sha3(key);
    }
    byte[] result = new byte[key.length];
    arraycopy(addrHash, 0, result, 0, PREFIX_BYTES);
    arraycopy(key, PREFIX_BYTES, result, PREFIX_BYTES, PREFIX_BYTES);
    return result;
  }

  // 32 bytes
  private static byte[] addrHash(byte[] address) {
    return Hash.sha3(address);
  }

  private static byte[] addrHash(byte[] address, byte[] trxHash) {
    if (ByteUtil.isNullOrZeroArray(trxHash)) {
      return Hash.sha3(address);
    }
    return Hash.sha3(ByteUtil.merge(address, trxHash));
  }

  public void generateAddrHash(byte[] trxId) {
    // update addreHash for create2
    addrHash = addrHash(address, trxId);
  }

  public DataWord getValue(DataWord key) {
    if (rowCache.containsKey(key)) {
      return new DataWord(rowCache.get(key).getValue());
    } else {
      StorageRowCapsule row = store.get(compose(key.getData(), addrHash));
      if (row == null || row.getInstance() == null) {
        return null;
      }
      rowCache.put(key, row);
      return new DataWord(row.getValue());
    }
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L451-518)
```java
  public void suicide(DataWord obtainerAddress) {

    byte[] owner = getContextAddress();
    byte[] obtainer = obtainerAddress.toTronAddress();

    if (VMConfig.allowTvmVote()) {
      withdrawRewardAndCancelVote(owner, getContractState());
    }

    long balance = getContractState().getBalance(owner);

    if (logger.isDebugEnabled()) {
      logger.debug("Transfer to: [{}] heritage: [{}]",
          Hex.toHexString(obtainer),
          balance);
    }

    increaseNonce();

    InternalTransaction internalTx = addInternalTx(null, owner, obtainer, balance, null,
        "suicide", nonce, getContractState().getAccount(owner).getAssetMapV2());

    int ADDRESS_SIZE = VMUtils.getAddressSize();
    if (FastByteComparisons.compareTo(owner, 0, ADDRESS_SIZE, obtainer, 0, ADDRESS_SIZE) == 0) {
      // if owner == obtainer just zeroing account according to Yellow Paper
      getContractState().addBalance(owner, -balance);
      byte[] blackHoleAddress = getContractState().getBlackHoleAddress();
      if (VMConfig.allowTvmTransferTrc10()) {
        getContractState().addBalance(blackHoleAddress, balance);
        MUtil.transferAllToken(getContractState(), owner, blackHoleAddress);
      }
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
    if (VMConfig.allowTvmFreeze()) {
      byte[] blackHoleAddress = getContractState().getBlackHoleAddress();
      if (FastByteComparisons.isEqual(owner, obtainer)) {
        transferDelegatedResourceToInheritor(owner, blackHoleAddress, getContractState());
      } else {
        transferDelegatedResourceToInheritor(owner, obtainer, getContractState());
      }
    }
    if (VMConfig.allowTvmFreezeV2()) {
      byte[] Inheritor =
          FastByteComparisons.isEqual(owner, obtainer)
              ? getContractState().getBlackHoleAddress()
              : obtainer;
      long expireUnfrozenBalance = transferFrozenV2BalanceToInheritor(owner, Inheritor, getContractState());
      if (expireUnfrozenBalance > 0 && internalTx != null) {
        internalTx.setValue(internalTx.getValue() + expireUnfrozenBalance);
      }
    }

    getContractState().markSelfDestruct(owner);
    getResult().addDeleteAccount(this.getContractAddress());
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L520-591)
```java
  public void suicide2(DataWord obtainerAddress) {
    byte[] owner = getContextAddress();

    if (getContractState().isSelfDestructed(obtainerAddress.toTronAddress())) {
      MUtil.checkCPUTimeForSelfDestructedBeneficiary();
    }

    boolean isNewContract = getContractState().isNewContract(owner);
    if (isNewContract) {
      suicide(obtainerAddress);
      return;
    }

    byte[] obtainer = obtainerAddress.toTronAddress();

    long balance = getContractState().getBalance(owner);

    if (logger.isDebugEnabled()) {
      logger.debug("Transfer to: [{}] heritage: [{}]",
          Hex.toHexString(obtainer),
          balance);
    }

    increaseNonce();

    InternalTransaction internalTx = addInternalTx(null, owner, obtainer, balance, null,
        "suicide", nonce, getContractState().getAccount(owner).getAssetMapV2());

    if (FastByteComparisons.isEqual(owner, obtainer)) {
      getContractState().markSelfDestruct(owner);
      return;
    }

    if (VMConfig.allowTvmVote()) {
      withdrawRewardAndCancelVote(owner, getContractState());
      balance = getContractState().getBalance(owner);
      if (internalTx != null && balance != internalTx.getValue()) {
        internalTx.setValue(balance);
      }
    }

    // transfer balance and trc10
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

    // transfer freeze
    if (VMConfig.allowTvmFreeze()) {
      transferDelegatedResourceToInheritor(owner, obtainer, getContractState());
    }

    // transfer freezeV2
    if (VMConfig.allowTvmFreezeV2()) {
      long expireUnfrozenBalance =
          transferFrozenV2BalanceToInheritor(owner, obtainer, getContractState());
      if (expireUnfrozenBalance > 0 && internalTx != null) {
        internalTx.setValue(internalTx.getValue() + expireUnfrozenBalance);
      }
    }

    getContractState().markSelfDestruct(owner);
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L821-882)
```java
  private void createContractImpl(DataWord value, byte[] programCode, byte[] newAddress,
      boolean isCreate2) {
    byte[] senderAddress = getContextAddress();

    if (logger.isDebugEnabled()) {
      logger.debug("creating a new contract inside contract run: [{}]",
          Hex.toHexString(senderAddress));
    }

    long endowment = value.value().longValueExact();
    if (getContractState().getBalance(senderAddress) < endowment) {
      stackPushZero();
      return;
    }

    AccountCapsule existingAccount = getContractState().getAccount(newAddress);
    boolean contractAlreadyExists = existingAccount != null;

    if (VMConfig.allowTvmConstantinople()) {
      contractAlreadyExists =
          contractAlreadyExists && isContractExist(existingAccount, getContractState());
    }
    Repository deposit = getContractState().newRepositoryChild();
    if (VMConfig.allowTvmConstantinople()) {
      if (existingAccount == null) {
        deposit.createAccount(newAddress, "CreatedByContract",
            AccountType.Contract);
      } else if (!contractAlreadyExists) {
        existingAccount.updateAccountType(AccountType.Contract);
        existingAccount.clearDelegatedResource();
        deposit.updateAccount(newAddress, existingAccount);
      }

      if (!contractAlreadyExists) {
        Builder builder = SmartContract.newBuilder();
        if (VMConfig.allowTvmCompatibleEvm()) {
          builder.setVersion(getContractVersion());
        }
        builder.setContractAddress(ByteString.copyFrom(newAddress))
            .setConsumeUserResourcePercent(100)
            .setOriginAddress(ByteString.copyFrom(senderAddress));
        if (isCreate2) {
          builder.setTrxHash(ByteString.copyFrom(rootTransactionId));
        }
        SmartContract newSmartContract = builder.build();
        deposit.createContract(newAddress, new ContractCapsule(newSmartContract));
      }
    } else {
      deposit.createAccount(newAddress, "CreatedByContract",
          Protocol.AccountType.Contract);
      Builder builder = SmartContract.newBuilder();
      if (VMConfig.allowTvmCompatibleEvm()) {
        builder.setVersion(getContractVersion());
      }
      SmartContract newSmartContract = builder.setContractAddress(ByteString.copyFrom(newAddress))
          .setConsumeUserResourcePercent(100)
          .setOriginAddress(ByteString.copyFrom(senderAddress)).build();
      deposit.createContract(newAddress, new ContractCapsule(newSmartContract));
      // In case of hashing collisions, check for any balance before createAccount()
      long oldBalance = deposit.getBalance(newAddress);
      deposit.addBalance(newAddress, oldBalance);
    }
```

**File:** framework/src/test/java/org/tron/common/runtime/vm/FreezeTest.java (L489-517)
```java
  @Test
  public void testCreate2SuicideToBlackHole() throws Exception {
    byte[] factory = deployContract("FactoryContract", FACTORY_CODE);
    byte[] contract = deployContract("TestFreeze", CONTRACT_CODE);
    long frozenBalance = 1_000_000;
    freezeForSelf(contract, frozenBalance, 0);
    freezeForSelf(contract, frozenBalance, 1);
    long salt = 1;
    byte[] predictedAddr = getCreate2Addr(factory, salt);
    freezeForOther(contract, predictedAddr, frozenBalance, 0);
    freezeForOther(contract, predictedAddr, frozenBalance, 1);
    Assert.assertArrayEquals(predictedAddr, deployCreate2Contract(factory, salt));
    setBalance(predictedAddr, 100_000_000);
    freezeForSelf(predictedAddr, frozenBalance, 0);
    freezeForSelf(predictedAddr, frozenBalance, 1);
    freezeForOther(predictedAddr, userA, frozenBalance, 0);
    freezeForOther(predictedAddr, userA, frozenBalance, 1);
    suicideWithException(predictedAddr, predictedAddr);
    clearDelegatedExpireTime(predictedAddr, userA);
    unfreezeForOther(predictedAddr, userA, 0);
    unfreezeForOther(predictedAddr, userA, 1);
    suicideToAccount(predictedAddr, predictedAddr);

    unfreezeForOtherWithException(contract, predictedAddr, 0);
    unfreezeForOtherWithException(contract, predictedAddr, 1);
    clearDelegatedExpireTime(contract, predictedAddr);
    unfreezeForOther(contract, predictedAddr, 0);
    unfreezeForOther(contract, predictedAddr, 1);
  }
```
