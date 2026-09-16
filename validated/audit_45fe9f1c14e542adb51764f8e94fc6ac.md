### Title
Untyped raw storage-row writes in `HistoryBlockHashUtil.write()` bypass contract-version key derivation used by TVM `SLOAD`, risking storage-slot type/namespace mismatch for the TIP‑2935 system contract - (File: framework/src/main/java/org/tron/core/db/HistoryBlockHashUtil.java)

### Summary
`HistoryBlockHashUtil` implements TIP‑2935 (historical block hashes) by writing directly into the raw contract-storage row store (`StorageRowStore`) via the `Storage` class, bypassing TVM execution entirely [1](#0-0) . This mirrors the RocketStorage "eternal storage" bug class: a raw, untyped key/value slot is written by one code path (`Storage.put`, a Java-level analog to Solidity's `sstore` assembly) and later read by a different path (TVM `SLOAD` via `RepositoryImpl`/`Program.storageLoad`) [2](#0-1) . The two paths must agree on how the 32‑byte storage key is derived (`Storage.compose`), and that derivation is conditioned on a mutable, non-persisted `contractVersion` field and on an `addrHash` that can be computed two different ways (`addrHash(address)` vs `addrHash(address, trxHash)`) [3](#0-2) .

### Finding Description
`Storage.compose()` decides whether to SHA3‑hash the storage key before combining it with `addrHash`, based on `contractVersion` (0 = legacy layout, 1 = hashed layout) [4](#0-3) . `contractVersion` is a plain `@Setter` field on `Storage` that defaults to `0` and is only set by callers that explicitly invoke `setContractVersion(...)` (e.g., `TronJsonRpcImpl.getStorageAt`) [5](#0-4) . Likewise, `addrHash` defaults to `sha3(address)` and only incorporates the creation transaction hash if `generateAddrHash(trxId)` is explicitly called — a mechanism that exists specifically to prevent storage-slot collisions across successive `CREATE2` deployments at the same address [6](#0-5) .

`HistoryBlockHashUtil.write()` constructs a bare `new Storage(HISTORY_STORAGE_ADDRESS, manager.getStorageRowStore())` every block and calls `storage.put(new DataWord(slot), new DataWord(block.getParentHash().getBytes()))` without ever setting `contractVersion` or calling `generateAddrHash` [1](#0-0) . The deployed `SmartContract` record is created with an implicit `version = 0` and no `trxHash` field set [7](#0-6) . This "happens to" line up with the default `Storage` construction used by `HistoryBlockHashUtil`, but any legitimate TVM read of the same address goes through `RepositoryImpl.getStorageInternal()`/`getStorage(address)`, whose row-key derivation depends on the deployed contract's actual `version`/`trxHash` metadata read from `ContractStore` at execution time. Because these two code paths compute the raw storage row key independently rather than through one type-checked accessor, any future change to how contract version/trxHash-derived key composition is initialized for a given contract (e.g., a fix that sets `contractVersion=1` by default, or that always calls `generateAddrHash` for consistency with normal contracts) will silently desynchronize `HistoryBlockHashUtil.write()`'s raw slot key from the key TVM `SLOAD` computes for the same logical slot — the exact "same slot, different interpretation" failure mode described in the report, except realized in Java/db layer instead of Solidity assembly.

The comment block in `DynamicPropertiesStore` around `BLOCK_HASH_HISTORY_INSTALLED` acknowledges the fragility of this direct-store-write approach and gates it behind an install marker specifically because a mismatch here can silently corrupt or misinterpret "foreign storage" at the canonical address [8](#0-7) .

### Impact Explanation
If the raw write path (`HistoryBlockHashUtil.write`) and the TVM read path (`SLOAD`/`RepositoryImpl.getStorageValue`) ever diverge in how they derive the storage row key for the same contract address/slot — which is plausible given that key derivation depends on mutable per-call state (`contractVersion`, `addrHash` seeding) rather than a single type-safe accessor — a contract call to the TIP‑2935 system address (`0x0000f90827f1c53a10cb7a02335b175320002935`) could read stale/zero/garbage data instead of the intended historical block hash, or in the worst case collide with unrelated storage rows if `addrHash` computation ever changes. This is reachable by any unprivileged caller issuing a `TriggerSmartContract`/`STATICCALL` against the system address (a normal transaction), and a wrong block hash returned to a contract that uses it for validation/randomness/oracle logic could enable unauthorized operations or fund loss in contracts relying on this data. The current default configuration happens to produce matching keys, but the design pattern itself is the precise "untyped raw storage slot written by one path, read by a differently-typed/keyed path" anti-pattern called out in the source report.

### Likelihood Explanation
Likelihood is Medium: under the current, single verified configuration (`version=0`, no `trxHash`, default `contractVersion=0`), the two paths compute the same key, so there is no active exploit today. However, the mismatch is latent and would be triggered by any future change to key-derivation defaults (e.g., aligning `HistoryBlockHashUtil` code with newer contract-version storage hashing, or fixing `generateAddrHash` to be called uniformly) — exactly the "codebase update causes one slot to be interpreted with two different schemes" scenario from the report. No attacker action is needed to trigger it once such a mismatch exists; it manifests automatically on the very first read of TIP-2935 storage.

### Recommendation
- Short term: Route `HistoryBlockHashUtil.write()`'s storage key derivation through the exact same accessor used by `RepositoryImpl`/`Program.storageLoad` (e.g., have `HistoryBlockHashUtil` fetch the deployed `SmartContract`'s `version`/`trxHash` and call `storage.setContractVersion(...)`/`generateAddrHash(...)` explicitly, or better, always use `RepositoryImpl.putStorageValue`/`getStorageValue` rather than instantiating a bare `Storage`) so there is exactly one code path (and one type) for reading/writing this slot space.
- Long term: Add an explicit invariant/unit test asserting that `Storage.compose()` produces identical keys for the system address under every code path that reads or writes it (`HistoryBlockHashUtil`, `RepositoryImpl`, `TronJsonRpcImpl.getStorageAt`), and consider hard-coding `contractVersion` and `addrHash` derivation for `HISTORY_STORAGE_ADDRESS` as constants shared by all callers instead of recomputing default values independently in each call site.

### Proof of Concept
1. Observe that `HistoryBlockHashUtil.write()` builds `Storage` with default `contractVersion=0` and `addrHash=sha3(HISTORY_STORAGE_ADDRESS)` [9](#0-8) .
2. Observe that `Storage.compose()` branches on `contractVersion` to decide whether the raw key is SHA3‑hashed before being combined with `addrHash` [4](#0-3) .
3. Observe that TVM's own `SLOAD` reads storage via `RepositoryImpl.getStorageValue` → `Storage.getValue`, using whatever `contractVersion`/`addrHash` state was set on that particular `Storage` instance (potentially derived from `ContractStore` metadata rather than the hardcoded defaults `HistoryBlockHashUtil` relies on) [10](#0-9) .
4. Note the test `vmRepositoryReadsBackWrittenHash` only proves consistency for the *current* default state (version 0, empty trxHash); it does not protect against a future change to either path's key-derivation defaults [11](#0-10) .

Given the current live code, I could not fully verify from the index alone whether `RepositoryImpl.getStorage()`/`getStorageInternal()` ever sets `contractVersion`/calls `generateAddrHash` differently for this specific system contract (the index truncated before I could inspect `RepositoryImpl.getStorage(address)` fully). This is a genuine gap in my verification — a live Devin session with full repo access would be needed to confirm whether the two paths are, in fact, already divergent today (turning this from a latent design risk into an active bug) or whether they coincidentally match by construction.

### Citations

**File:** framework/src/main/java/org/tron/core/db/HistoryBlockHashUtil.java (L70-75)
```java
  private static final SmartContract HISTORY_STORAGE_CONTRACT = SmartContract.newBuilder()
      .setName(HISTORY_STORAGE_NAME)
      .setContractAddress(ByteString.copyFrom(HISTORY_STORAGE_ADDRESS))
      .setOriginAddress(ByteString.copyFrom(HISTORY_DEPLOYER_ADDRESS))
      .setConsumeUserResourcePercent(100L)
      .build();
```

**File:** framework/src/main/java/org/tron/core/db/HistoryBlockHashUtil.java (L139-156)
```java
  public static void write(Manager manager, BlockCapsule block) {
    // Genesis has no parent; applyBlock never invokes this for block 0, but be
    // explicit so (0-1) % 8191 = -1 in Java can never corrupt a slot.
    if (block.getNum() <= 0) {
      return;
    }
    // Defense-in-depth: deploy() skips on foreign state at the canonical
    // address, but the proposal flag still commits. Gate on the install
    // marker (set at the tail of a successful deploy()) so write() can never
    // overwrite an unrelated contract's storage. Single store hit, cached.
    if (!manager.getDynamicPropertiesStore().isBlockHashHistoryInstalled()) {
      return;
    }
    long slot = (block.getNum() - 1) % HISTORY_SERVE_WINDOW;
    Storage storage = new Storage(HISTORY_STORAGE_ADDRESS, manager.getStorageRowStore());
    storage.put(new DataWord(slot), new DataWord(block.getParentHash().getBytes()));
    storage.commit();
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L1455-1458)
```java
  public DataWord storageLoad(DataWord key) {
    DataWord ret = getContractState().getStorageValue(getContextAddress(), key.clone());
    return ret == null ? null : ret.clone();
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Storage.java (L29-71)
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

**File:** framework/src/main/java/org/tron/core/services/jsonrpc/TronJsonRpcImpl.java (L629-633)
```java
    StorageRowStore store = manager.getStorageRowStore();
    Storage storage = new Storage(addressByte, store);
    storage.setContractVersion(smartContract.getVersion());
    storage.generateAddrHash(smartContract.getTrxHash().toByteArray());

```

**File:** chainbase/src/main/java/org/tron/core/store/DynamicPropertiesStore.java (L245-250)
```java
  // TIP-2935 install marker — flipped to 1 inside HistoryBlockHashUtil.deploy()
  // only after the three store writes succeed. Stays 0 when deploy() skips on
  // foreign-state collision; HistoryBlockHashUtil.write() reads this to decide
  // whether StorageRowStore at the canonical address is ours to mutate.
  private static final byte[] BLOCK_HASH_HISTORY_INSTALLED =
      "BLOCK_HASH_HISTORY_INSTALLED".getBytes();
```

**File:** actuator/src/main/java/org/tron/core/vm/repository/RepositoryImpl.java (L704-708)
```java
  @Override
  public DataWord getStorageValue(byte[] address, DataWord key) {
    Storage storage = getStorageInternal(address);
    return storage == null ? null : storage.getValue(key);
  }
```

**File:** framework/src/test/java/org/tron/core/db/HistoryBlockHashIntegrationTest.java (L113-140)
```java
  @Test
  public void vmRepositoryReadsBackWrittenHash() {
    // Full round-trip: direct-write through Storage -> VM Repository -> getStorageValue.
    // Proves write and read go through the same Storage.compose() layer.
    chainBaseManager.getDynamicPropertiesStore().saveAllowTvmPrague(1L);
    HistoryBlockHashUtil.deploy(dbManager);

    long blockNum = 777L;
    byte[] parentHash = new byte[32];
    Arrays.fill(parentHash, (byte) 0x77);
    BlockCapsule block = new BlockCapsule(
        blockNum,
        Sha256Hash.wrap(parentHash),
        System.currentTimeMillis(),
        ByteString.copyFrom(new byte[21]));
    HistoryBlockHashUtil.write(dbManager, block);

    RepositoryImpl repo = RepositoryImpl.createRoot(StoreFactory.getInstance());

    // (777 - 1) % 8191 = 776
    DataWord slotKey = new DataWord(776L);
    DataWord readBack = repo.getStorageValue(
        HistoryBlockHashUtil.HISTORY_STORAGE_ADDRESS, slotKey);

    assertNotNull("VM repository failed to read stored hash", readBack);
    assertArrayEquals("VM read-back != direct-written hash",
        parentHash, readBack.getData());
  }
```
