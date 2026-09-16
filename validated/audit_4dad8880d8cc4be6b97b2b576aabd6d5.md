### Title
Contract storage rows are never deleted when a contract is destroyed, allowing a redeployed contract at a colliding address to inherit the previous occupant's storage data - ([File: actuator/src/main/java/org/tron/core/vm/repository/RepositoryImpl.java])

### Summary
The CVE describes ScaleIO thin volumes being handed to a new tenant without zero-padding, so the new volume exposes the previous tenant's data. The analogous flaw in java-tron is that `RepositoryImpl.deleteContract()` removes an account's code, account record and contract record, but never clears its `StorageRowStore` entries. If a new contract is subsequently created at the same on-chain address (an internal `CREATE` address collision, a case the code itself explicitly anticipates and special-cases), the new contract inherits the storage rows written by the previous occupant, because the `Storage` key derivation (`addrHash = sha3(address)`) for that path is independent of the deployment transaction.

### Finding Description
`Storage` composes on-disk keys as `sha3(address)` (or, only for CREATE2 deployments, `sha3(address ++ trxHash)`): [1](#0-0) 

`RepositoryImpl.getStorage()` only re-derives the address hash from the deploying transaction (`generateAddrHash`) when the contract has a non-zero `trxHash`, i.e. only for CREATE2: [2](#0-1) 

For ordinary internal contract creation via the `CREATE` opcode, `Program.createContractImpl()` explicitly acknowledges that the derived `newAddress` can already exist ("hashing collisions") and preserves any pre-existing balance at that address rather than rejecting the deployment: [3](#0-2) 

However, `deleteContract()` (invoked whenever a contract self-destructs) only removes the code, account and contract metadata - it never touches `storageRowStore`: [4](#0-3) 

Because the storage key namespace (`sha3(address)`) is identical for the old, destroyed contract and any new contract later occupying the same address (outside the CREATE2 `trxHash`-scoped path), any storage slot the new contract does not explicitly overwrite will still return the previous occupant's value via `Storage.getValue()`: [5](#0-4) 

This is the same root-cause pattern as CVE-2017-15139: a storage resource (ScaleIO thin volume / TRON contract storage slot) is handed to a new consumer (new tenant / new contract) without being zeroed, so uninitialized reads return stale data belonging to a different, prior owner.

### Impact Explanation
A contract deployer who redeploys at a colliding/self-destructed address can have their new contract silently read leftover values (balances flags, access-control booleans, previously stored secrets, prices, allowances, etc.) written by an unrelated prior contract that occupied the same address. Depending on how the new contract's logic branches on uninitialized-looking storage, this can lead to unauthorized account operations (e.g., a flag that was left "true" by the old occupant granting privileged behavior the new contract never set), or leakage of sensitive application data between different contract owners.

### Likelihood Explanation
Exploitation requires triggering an internal `CREATE` address collision at a previously self-destructed address, which the codebase's own comment ("In case of hashing collisions, check for any balance before createAccount()") shows is a recognized, reachable condition for the non-Constantinople internal-CREATE path, reachable purely through ordinary contract-deployment/self-destruct transactions from an unprivileged deployer - no special/privileged access is required.

### Recommendation
On contract self-destruction/`deleteContract()`, also purge all `StorageRowStore` rows keyed under that address's `addrHash`, or unconditionally scope the storage-row key derivation to the deploying transaction hash (as already done for CREATE2) so that no two contract incarnations at the same address can ever share a storage namespace.

### Proof of Concept
Conceptual reproduction (exact byte-level collision inputs would need to be confirmed with a live node/test harness, since deriving a concrete colliding `(sender, nonce)` internal-CREATE address requires brute-force or a lucky collision, which could not be verified further given available tooling):
1. Deploy Contract A via internal `CREATE` (from a factory contract) at address `X`; have Contract A write a sensitive value into a storage slot.
2. Self-destruct Contract A (`SELFDESTRUCT`), which calls `deleteContract(X)`, removing account/code/contract records but leaving `StorageRowStore` rows for `sha3(X)` intact.
3. Deploy Contract B via the same factory/internal `CREATE` path such that the computed `newAddress` collides with `X` (the code path in `Program.createContractImpl()` explicitly handles this "hashing collision" case by preserving balance rather than rejecting the deployment).
4. Contract B reads an uninitialized storage slot that Contract A had written; it returns Contract A's leftover value instead of zero, confirming cross-contract data leakage through the shared storage-row namespace.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/program/Storage.java (L56-71)
```java
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
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Storage.java (L73-84)
```java
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

**File:** actuator/src/main/java/org/tron/core/vm/repository/RepositoryImpl.java (L487-492)
```java
  @Override
  public void deleteContract(byte[] address) {
    getCodeStore().delete(address);
    getAccountStore().delete(address);
    getContractStore().delete(address);
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/repository/RepositoryImpl.java (L726-752)
```java
  @Override
  public Storage getStorage(byte[] address) {
    Key key = Key.create(address);
    if (storageCache.containsKey(key)) {
      return storageCache.get(key);
    }
    Storage storage;
    if (this.parent != null) {
      Storage parentStorage = parent.getStorage(address);
      if (StorageUtils.getEnergyLimitHardFork()) {
        // deep copy
        storage = new Storage(parentStorage);
      } else {
        storage = parentStorage;
      }
    } else {
      storage = new Storage(address, getStorageRowStore());
    }
    ContractCapsule contract = getContract(address);
    if (contract != null) {
      storage.setContractVersion(contract.getContractVersion());
      if (!ByteUtil.isNullOrZeroArray(contract.getTrxHash())) {
        storage.generateAddrHash(contract.getTrxHash());
      }
    }
    return storage;
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L868-882)
```java
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
