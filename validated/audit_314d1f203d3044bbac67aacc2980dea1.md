### Title
Self-destructed contract's key-value storage (TVM "mapping" state) is never erased on deletion, allowing stale storage to be inherited by a redeployed contract at the same address - ([File: actuator/src/main/java/org/tron/core/vm/repository/RepositoryImpl.java])

### Summary
`RepositoryImpl.deleteContract()` — invoked when a contract is destroyed via `SELFDESTRUCT`/`suicide` — only removes the account, contract-metadata and code entries, but never clears the contract's `StorageRowStore` rows (the TVM analogue of Solidity mappings/state variables). This mirrors the reported Solidity bug class: deleting a struct/record leaves its mapping-backed data intact and reachable.

### Finding Description
`RepositoryImpl.deleteContract(byte[] address)` deletes only three stores: [1](#0-0) 

It does not touch the per-contract `Storage`/`StorageRowStore`, which is where all of a contract's persistent state variables (including Solidity `mapping` types) are stored, keyed by `addrHash(address)` derived purely from the contract address: [2](#0-1) 

`Storage.commit()` only deletes a row when it is explicitly overwritten with a zero value (`row.isDirty()` and value is zero); it never bulk-purges all rows for an address on contract deletion: [3](#0-2) 

When `SELFDESTRUCT` executes, `Program.suicide`/`suicide2` transfer balance/TRC10/frozen resources and mark the address as self-destructed, but never clear `Storage` rows either — they only call `markSelfDestruct` and register the address for later `deleteContract`: [4](#0-3) 

Since contract addresses can be deterministically predicted and reused with `CREATE2` (as exercised by the codebase's own tests), a contract can be self-destructed and a new contract subsequently deployed to the exact same address; the new contract's `SLOAD`s at slots that were previously written by the destroyed contract will return the old, "deleted" values because the underlying `StorageRowStore` rows were never purged, only the account/contract/code records were.

### Impact Explanation
Any deployer can self-destruct a contract and redeploy at the same address via `CREATE2`. If the new contract logic assumes fresh/zeroed storage (as `delete`d Solidity state normally implies), it will instead silently read pre-existing "leftover" storage values from the prior incarnation of the contract, which can be manipulated by the attacker who controlled the original contract before self-destructing it. This can lead to unauthorized account/contract state operations (e.g., an attacker seeding slots used later as owner/permission/mapping flags, or balances/allowances tracked in contract storage) once a victim or automated factory redeploys at the predictable `CREATE2` address, potentially enabling theft of funds or privilege bypass in the redeployed contract.

### Likelihood Explanation
Requires the attacker to control the destroyed contract's deployment and to know/predict the `CREATE2` address reused by a subsequent deployment (a common pattern for factory contracts and counterfactual deployments). This is a realistic, single-transaction-reachable pattern already exercised in java-tron's own test suite (`CreateContractSuicideTest`, `FreezeTest#testCreate2SuicideToBlackHole`), showing self-destruct followed by redeployment at a predicted `CREATE2` address is a supported and expected flow, not an edge case.

### Recommendation
When `deleteContract` (or the self-destruct account-deletion path) processes a destroyed contract, also purge all `StorageRowStore` rows keyed by that contract's `addrHash`, or otherwise ensure any redeployment to the same address (via `CREATE2` or address reuse) results in a fresh/zeroed storage view rather than inheriting rows from the prior contract instance.

### Proof of Concept
1. Deploy `ContractA` at a `CREATE2`-predicted address; have it write nonzero values to storage slots (e.g., a mapping entry / owner flag).
2. Call `selfdestruct` on `ContractA` (reachable via a normal `TriggerSmartContract`), which invokes `Program.suicide`/`suicide2` and eventually `RepositoryImpl.deleteContract`, deleting only account/contract/code entries.
3. Deploy `ContractB` (different bytecode/logic) to the same `CREATE2` address.
4. Read storage slots in `ContractB` that were written by `ContractA` — they return the old, pre-existing values instead of zero, because `RepositoryImpl.deleteContract` ( [1](#0-0) ) never invoked `Storage` row cleanup for the address.

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

**File:** actuator/src/main/java/org/tron/core/vm/program/Storage.java (L29-58)
```java
  public Storage(byte[] address, StorageRowStore store) {
    addrHash = addrHash(address);
    this.address = address;
    this.store = store;
  }

  public Storage(Storage storage) {
    this.addrHash = storage.addrHash.clone();
    this.address = storage.getAddress().clone();
    this.store = storage.store;
    this.contractVersion = storage.contractVersion;
    storage.getRowCache().forEach((DataWord rowKey, StorageRowCapsule row) -> {
      StorageRowCapsule newRow = new StorageRowCapsule(row);
      this.rowCache.put(rowKey.clone(), newRow);
    });
  }

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
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Storage.java (L96-106)
```java
  public void commit() {
    rowCache.forEach((DataWord rowKey, StorageRowCapsule row) -> {
      if (row.isDirty()) {
        if (new DataWord(row.getValue()).isZero()) {
          this.store.delete(row.getRowKey());
        } else {
          this.store.put(row.getRowKey(), row);
        }
      }
    });
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
