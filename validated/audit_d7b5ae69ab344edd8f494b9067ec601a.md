Based on my analysis, this bug class (storage slot addressing changing between contract "versions," causing existing data to be silently unreachable) does have a concrete analog in java-tron's TVM storage layer, in `Storage.compose()`.

### Title
Contract storage rows become permanently unreadable if a contract's `contractVersion` flag diverges from the value used when the storage was written - ([File: actuator/src/main/java/org/tron/core/vm/program/Storage.java])

### Summary
`Storage.compose()` determines the on-disk row key for every `SSTORE`/`SLOAD` by branching on `contractVersion`: for version `1` (EVM-compatible contracts) the 32-byte key is first hashed with `Hash.sha3`, while for version `0` the raw key is used directly. This is conceptually the same class of bug as the reported `GNSMultiCollatDiamond` issue: a single flag that determines how a mapping/array key is transformed into a storage slot, where any inconsistency between "write time" and "read time" values of that flag makes every previously written value permanently unreachable (and reads then silently return zero/empty instead of erroring).

### Finding Description
`Storage.compose()` is the single choke-point translating a `DataWord` key plus `addrHash` into the row key persisted in `StorageRowStore`: [1](#0-0) 

`contractVersion` is set once per `Storage` instance from `ContractCapsule.getContractVersion()` (i.e., `SmartContract.version`), which itself is fixed at contract creation time in `VMActuator.create()`: [2](#0-1) 

and propagated into the `Program`/`Storage` objects: [3](#0-2) 

and re-derived on every subsequent access via `RepositoryImpl.getStorage()`: [4](#0-3) 

and in the JSON-RPC `eth_getStorageAt` path: [5](#0-4) 

Exactly like the `GNSMultiCollatDiamond` bug — where `accessControl`'s slot number depended on the layout of a preceding struct and any layout drift silently reroutes all mapping lookups to the wrong slot — here every stored value's row key depends entirely on the `contractVersion` bit being read consistently every time the contract's storage is touched. If any code path ever computed or reported a contract's version inconsistently with what was in effect when its storage was written (for example, a contract deployed under one `VMConfig.allowTvmCompatibleEvm()` setting, then having its stored `SmartContract.version` field misread, cleared, or defaulted differently by a future feature/migration touching `ContractCapsule`/`SmartContract.version`), then `compose()` would hash (or fail to hash) keys differently than at write time, and `getValue()` would silently return `null`/zero for every existing slot — functionally identical to "losing all current roles/state after upgrade," but here it is exploitable/reachable for any deployed TVM contract's persistent state, not merely admin roles.

### Impact Explanation
If storage-key derivation for a live contract ever becomes inconsistent with the value baked in at deploy time, every existing storage slot for that contract becomes permanently unreadable through normal `SLOAD`/`getStorageAt`, while writes continue to land at the new (wrong) location. This is a silent, permanent loss of contract state — equivalent in class to the "all role-gated functionality inaccessible" impact in the report, but generalized to any TVM contract's persisted variables/mappings, which can include balances/accounting tracked in contract storage, not just access-control data.

### Likelihood Explanation
This report's underlying root cause requires a concrete divergence in how `contractVersion` is computed or stored between write-time and read-time. My review of the current codebase found `contractVersion` set once at deployment (`VMActuator.create()`, `ContractCapsule.getContractVersion()`) and consistently re-derived from `ContractStore` on every subsequent read (`RepositoryImpl.getStorage()`, `TronJsonRpcImpl.getStorageAt()`) — I did not find an existing code path in this snapshot that actually mutates or misreads `SmartContract.version` after deployment (e.g., `UpdateSettingContractActuator` only touches `consumeUserResourcePercent`, not `version`). Because I could not locate a concrete reachable trigger that flips this flag inconsistently today, I cannot confirm this is currently exploitable as-is; it stands only as an architecturally analogous single-point-of-failure that should be reviewed whenever changes are made to `SmartContract.version` handling, contract migration/upgrade tooling, or TVM hard-fork flags that gate `contractVersion`.

### Recommendation
Treat `contractVersion` the same way the original report recommends treating storage layout: never allow its derivation to change for an already-deployed contract. Concretely: (1) ensure `SmartContract.version` is immutable once written to `ContractStore` and add explicit validation/tests asserting it cannot be cleared/overwritten by any actuator or migration; (2) add a regression test that writes storage under one `contractVersion`, then re-reads it forcing the other `contractVersion` value, to prove `compose()` diverges and document this as a protocol invariant; (3) avoid deriving `contractVersion` from mutable config in any new code path — only from the immutable per-contract stored value.

### Proof of Concept
Conceptual reproduction using existing test infrastructure (`framework/src/test/java/org/tron/common/runtime/vm/StorageTest.java`) as a template:
1. Deploy a contract, write a value via `rootRepository.putStorageValue(address, key, value)` while `Storage.contractVersion == 0`.
2. Commit and construct a fresh `Storage` instance for the same address using `setContractVersion(1)` (simulating a version-tracking inconsistency).
3. Call `getValue(key)` and observe it returns `null`/empty instead of the previously stored value, because `compose()` now hashes the key with `Hash.sha3` before the raw comparison used at write time — demonstrating the same "silently lost state after key-derivation change" effect described in the external report. [6](#0-5)

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/program/Storage.java (L46-54)
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
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Storage.java (L73-94)
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

  public void put(DataWord key, DataWord value) {
    if (rowCache.containsKey(key)) {
      rowCache.get(key).setValue(value.getData());
    } else {
      byte[] rowKey = compose(key.getData(), addrHash);
      StorageRowCapsule row = new StorageRowCapsule(rowKey, value.getData());
      rowCache.put(key, row);
    }
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/VMActuator.java (L340-345)
```java
    SmartContract newSmartContract;
    if (VMConfig.allowTvmCompatibleEvm()) {
      newSmartContract = contract.getNewContract().toBuilder().setVersion(1).build();
    } else {
      newSmartContract = contract.getNewContract().toBuilder().clearVersion().build();
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/VMActuator.java (L426-429)
```java
      this.program = new Program(ops, contractAddress, programInvoke, rootInternalTx);
      if (VMConfig.allowTvmCompatibleEvm()) {
        this.program.setContractVersion(1);
      }
```

**File:** actuator/src/main/java/org/tron/core/vm/repository/RepositoryImpl.java (L744-750)
```java
    ContractCapsule contract = getContract(address);
    if (contract != null) {
      storage.setContractVersion(contract.getContractVersion());
      if (!ByteUtil.isNullOrZeroArray(contract.getTrxHash())) {
        storage.generateAddrHash(contract.getTrxHash());
      }
    }
```

**File:** framework/src/main/java/org/tron/core/services/jsonrpc/TronJsonRpcImpl.java (L629-632)
```java
    StorageRowStore store = manager.getStorageRowStore();
    Storage storage = new Storage(addressByte, store);
    storage.setContractVersion(smartContract.getVersion());
    storage.generateAddrHash(smartContract.getTrxHash().toByteArray());
```
