## Title
No-op cache-invalidation hook `RepositoryImpl.removeLruCache` allows stale account/contract data to be served from the LRU cache after SELFDESTRUCT/contract updates - (File: `actuator/src/main/java/org/tron/core/vm/repository/RepositoryImpl.java`)

### Summary
`RepositoryImpl` exposes a static hook `removeLruCache(byte[] address)` that is explicitly invoked after mutating on-chain state outside the normal cached write path (self-destructed accounts, updated contract energy limits/settings), but the method body is empty, so the invalidation never happens. [1](#0-0) 

### Finding Description
`RepositoryImpl` maintains an in-transaction write-set of caches (`accountCache`, `contractCache`, etc.) that get flushed to the underlying stores on `commit()`. [2](#0-1) 

Underneath the stores, `SnapshotRoot` additionally maintains a process-wide `TronCache` (Guava-based LRU cache) for the `account`/`contract`/etc. databases, allocated via `CacheManager`/`CacheStrategies` (`account` and `storageRow` use the largest LRU size, 20000 entries, `CACHE_HUGE_DBS`). [3](#0-2) [4](#0-3) 

Every normal `put`/`remove` on `SnapshotRoot` refreshes this LRU cache (`putCache(key, v)` / `putCache(key, null)`). [5](#0-4) 

However, three call sites explicitly bypass that normal refresh and instead try to force-invalidate the LRU entry via `RepositoryImpl.removeLruCache(address)`:
- `VMActuator`, after a TVM execution completes and `rootRepository.commit()` has run, it iterates `result.getDeleteAccounts()` (populated by `SELFDESTRUCT`/`suicide`) and calls `RepositoryImpl.removeLruCache(account.toTronAddress())` for every self-destructed account. [6](#0-5) 
- `UpdateEnergyLimitContractActuator`, after writing a new `originEnergyLimit` directly to `contractStore`, calls `RepositoryImpl.removeLruCache(contractAddress)` to force a fresh read of the contract on next access. [7](#0-6) 
- `UpdateSettingContractActuator` follows the identical pattern for the `consumeUserResourcePercent` setting.

In all three cases, the intent is unmistakable: state was changed via a path that does not automatically refresh the LRU cache, so the code explicitly attempts to evict/refresh the stale entry. But `removeLruCache` is a no-op stub:
```java
public static void removeLruCache(byte[] address) {
}
``` [1](#0-0) 

This exactly mirrors the CVE-2022-50822 bug class ("resource must be released/invalidated on delete, otherwise stale/leaked state persists") — here the "resource" is a cached account/contract entry in the process-wide LRU (`TronCache`) rather than a kernel restrack object, but the effect is the same: the invalidation call site exists and is exercised on every attacker-reachable `SELFDESTRUCT` and every `UpdateEnergyLimitContract`/`UpdateSettingContract` transaction, yet performs no work, leaving stale cached account/contract data reachable by subsequent reads through `SnapshotRoot.get()`, which serves the LRU cache before falling back to RocksDB. [8](#0-7) 

### Impact Explanation
Because the cache-invalidation hook silently no-ops, whenever a contract self-destructs (an operation any unprivileged transaction sender can trigger by deploying and invoking a contract with `SELFDESTRUCT`), or whenever `UpdateEnergyLimitContract`/`UpdateSettingContract` is broadcast, the previously-cached account/contract data at that address can remain served by the huge (20000-entry, 30s TTL) `account`/`contract` `TronCache` LRU instead of the freshly committed state. Depending on timing of subsequent reads within the cache's staleness window, this can surface outdated balances, energy limits, or resource-consumption settings to the rest of the node (including anything reading via `AccountStore.get`/`ContractStore.get`, i.e. TVM opcodes, actuators, and Wallet/JSON-RPC query paths), producing incorrect account/energy accounting until the entry is evicted or overwritten by a normal `put`. This is a state-consistency/accounting-correctness defect class (High severity, matching the analog's memory/consistency-leak nature) rather than a simple resource leak, since java-tron's stores are consensus-critical.

### Likelihood Explanation
The bug is trivially reachable: `SELFDESTRUCT` inside any smart contract deployed by an unprivileged account exercises the `VMActuator` code path unconditionally on every self-destruct, and `UpdateEnergyLimitContract`/`UpdateSettingContract` are ordinary broadcastable contract types. No special privilege, malicious SR/witness, or network condition is required — a single crafted transaction sequence (deploy self-destructing contract → call it → immediately re-read the account/contract) is sufficient to hit the stale-cache path.

### Recommendation
Implement `RepositoryImpl.removeLruCache` to actually invalidate the corresponding entry in the `account`/`contract` `TronCache` (e.g., via `CacheManager`'s allocated cache for `CacheType.account`/`CacheType.contract`, calling `cache.invalidate(WrappedByteArray.of(address))`), rather than leaving it as an empty stub. Add regression tests that verify a `SELFDESTRUCT`ed account and an `UpdateEnergyLimitContract`-updated contract are not served stale data from the LRU cache immediately after the mutating transaction commits.

### Proof of Concept
1. Deploy a contract `C` at address `A` and fund it so `AccountStore.get(A)` is read once (populating the `account` LRU cache with the funded balance).
2. Call `C`'s function to trigger `SELFDESTRUCT`, transferring the balance elsewhere; `VMActuator` commits the zeroed/self-destructed account and then calls `RepositoryImpl.removeLruCache(A)`, which is a no-op. [6](#0-5) 
3. Immediately issue a read for address `A` through any path that goes through `SnapshotRoot.get()` (e.g. a follow-up transaction or API query) within the 30s cache TTL; observe that stale (pre-self-destruct) cached data can still be returned instead of the freshly committed state, since `getCache(key)` is checked before consulting the underlying RocksDB. [8](#0-7)

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/repository/RepositoryImpl.java (L127-145)
```java
  private final HashMap<Key, Value<Account>> accountCache = new HashMap<>();
  private final HashMap<Key, Value<byte[]>> codeCache = new HashMap<>();
  private final HashMap<Key, Value<SmartContract>> contractCache = new HashMap<>();
  private final HashMap<Key, Value<ContractState>> contractStateCache
      = new HashMap<>();
  private final HashMap<Key, Storage> storageCache = new HashMap<>();

  private final HashMap<Key, Value<AssetIssueContract>> assetIssueCache = new HashMap<>();
  private final HashMap<Key, Value<byte[]>> dynamicPropertiesCache = new HashMap<>();
  private final HashMap<Key, Value<DelegatedResource>> delegatedResourceCache = new HashMap<>();
  private final HashMap<Key, Value<Votes>> votesCache = new HashMap<>();
  private final HashMap<Key, Value<byte[]>> delegationCache = new HashMap<>();
  private final HashMap<Key, Value<DelegatedResourceAccountIndex>> delegatedResourceAccountIndexCache = new HashMap<>();
  private final HashBasedTable<Key, Key, Value<byte[]>> transientStorage = HashBasedTable.create();
  private final HashSet<Key> newContractCache = new HashSet<>();
  private final HashSet<Key> selfDestructCache = new HashSet<>();

  public static void removeLruCache(byte[] address) {
  }
```

**File:** chainbase/src/main/java/org/tron/core/db2/core/SnapshotRoot.java (L24-42)
```java
public class SnapshotRoot extends AbstractSnapshot<byte[], byte[]> {

  @Getter
  private Snapshot solidity;
  private boolean isAccountDB;

  private TronCache<WrappedByteArray, WrappedByteArray> cache;
  private static final List<String> CACHE_DBS = CommonParameter.getInstance()
      .getStorage().getCacheDbs();

  public SnapshotRoot(DB<byte[], byte[]> db) {
    this.db = db;
    solidity = this;
    isAccountDB = "account".equalsIgnoreCase(db.getDbName());
    if (CACHE_DBS.contains(this.db.getDbName())) {
      this.cache = CacheManager.allocate(CacheType.findByType(this.db.getDbName()));
    }
    isOptimized = "properties".equalsIgnoreCase(db.getDbName());
  }
```

**File:** chainbase/src/main/java/org/tron/core/db2/core/SnapshotRoot.java (L49-58)
```java
  @Override
  public byte[] get(byte[] key) {
    WrappedByteArray cache = getCache(key);
    if (cache != null) {
      return cache.getBytes();
    }
    byte[] value = db.get(key);
    putCache(key, value);
    return value;
  }
```

**File:** chainbase/src/main/java/org/tron/core/db2/core/SnapshotRoot.java (L60-90)
```java
  @Override
  public void put(byte[] key, byte[] value) {
    byte[] v = value;
    if (needOptAsset()) {
      if (ByteArray.isEmpty(value)) {
        remove(key);
        return;
      }
      AccountAssetStore assetStore =
              ChainBaseManager.getInstance().getAccountAssetStore();
      AccountCapsule item = new AccountCapsule(value);
      if (!item.getAssetOptimized()) {
        assetStore.deleteAccount(item.createDbKey());
        item.setAssetOptimized(true);
      }
      assetStore.putAccount(item.getInstance());
      item.clearAsset();
      v = item.getData();
    }
    db.put(key, v);
    putCache(key, v);
  }

  @Override
  public void remove(byte[] key) {
    if (needOptAsset()) {
      ChainBaseManager.getInstance().getAccountAssetStore().deleteAccount(key);
    }
    db.remove(key);
    putCache(key, null);
  }
```

**File:** common/src/main/java/org/tron/common/cache/CacheStrategies.java (L41-47)
```java
  private static final List<CacheType> CACHE_NORMAL_DBS = Arrays.asList(code, contract,
      assetIssueV2, properties);
  private static final String CACHE_STRATEGY_BIG_DEFAULT =
      String.format(PATTERNS, 10000, 10000, "30s", CPUS);
  private static final String CACHE_STRATEGY_HUGE_DEFAULT =
      String.format(PATTERNS, 20000, 20000, "30s", CPUS);
  private static final List<CacheType> CACHE_HUGE_DBS = Arrays.asList(storageRow, account);
```

**File:** actuator/src/main/java/org/tron/core/actuator/VMActuator.java (L264-270)
```java
        }
      } else {
        rootRepository.commit();
      }
      for (DataWord account : result.getDeleteAccounts()) {
        RepositoryImpl.removeLruCache(account.toTronAddress());
      }
```

**File:** actuator/src/main/java/org/tron/core/actuator/UpdateEnergyLimitContractActuator.java (L43-48)
```java
      ContractCapsule deployedContract = contractStore.get(contractAddress);

      contractStore.put(contractAddress, new ContractCapsule(
          deployedContract.getInstance().toBuilder().setOriginEnergyLimit(newOriginEnergyLimit)
              .build()));
      RepositoryImpl.removeLruCache(contractAddress);
```
