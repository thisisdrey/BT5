I have enough evidence to write the final answer now.

### Title
Per-transaction O(N²) HashDoS via grindable base-31 hash-bucket collisions in TVM SLOAD/SSTORE storage-row cache and EIP-1153-style transient-storage tracking - ([File: actuator/src/main/java/org/tron/core/vm/program/Storage.java])

### Summary
`Storage.rowCache` (backing every `SLOAD`/`SSTORE`) and `RepositoryImpl`'s per-transaction caches — including `transientStorage` (the `TLOAD`/`TSTORE`, EIP-1153-equivalent map) — are all `HashMap`/`HashBasedTable` collections keyed by `DataWord` or `Key`. Both key types compute their `hashCode()` via `java.util.Arrays.hashCode`/`ArrayUtils.hashCode`, the classic grindable base-31 rolling hash, and neither is stored in a tree-ordered collection. An attacker who fully controls a contract's storage/transient-storage key operands can grind keys that collide into the same hash bucket, degrading per-op cache lookups from O(1) to O(n) and turning a single transaction's linear sequence of `SLOAD`/`SSTORE`/`TLOAD`/`TSTORE` operations into O(n²) work — the exact bug class described in the Besu advisory (grindable hash + HashSet/HashMap with no treeification fallback).

### Finding Description
- `Storage.rowCache` is declared as `Map<DataWord, StorageRowCapsule> rowCache = new HashMap<>()` and is read/written on every `getValue`/`put` call, i.e. on every `SLOAD`/`SSTORE` in a transaction: [1](#0-0) [2](#0-1) 
- `DataWord.hashCode()` is `java.util.Arrays.hashCode(data)`, the standard 31-multiplier rolling hash over the 32-byte storage key, which is fully attacker-computable/grindable since the key is supplied directly by contract bytecode (`PUSH .. SLOAD/SSTORE`): [3](#0-2) 
- `RepositoryImpl` maintains a parallel set of per-transaction `HashMap<Key, ...>` caches (`accountCache`, `codeCache`, `contractCache`, `contractStateCache`, `storageCache`, `assetIssueCache`, `delegatedResourceCache`, `votesCache`, `delegationCache`, `delegatedResourceAccountIndexCache`) plus a `HashBasedTable<Key, Key, Value<byte[]>> transientStorage` (the EIP-1153 `TLOAD`/`TSTORE` analog) and `HashSet<Key>` caches for `newContractCache`/`selfDestructCache`: [4](#0-3) 
- `Key.hashCode()` uses `ArrayUtils.hashCode(data)` — the same grindable base-31 hash algorithm as `Arrays.hashCode`, with no `Comparable`/tree ordering implemented at all, so `HashMap`/`HashBasedTable` bucket lookups can never be replaced by balanced-tree fallback: [5](#0-4) 
- `TLOAD`/`TSTORE` opcodes directly exercise `transientStorage` via `getTransientStorageValue`/`updateTransientStorageValue`, both keyed by the attacker-chosen `key` operand wrapped in `Key`: [6](#0-5) [7](#0-6) 

An attacker crafting a contract can offline-grind a sequence of 32-byte storage/transient-storage keys that all collide into the same hash bucket (trivial with the linear 31-multiplier construction — the same class of attack historically used against Java's default `HashMap` string/array hashing). Deploying/calling that contract with a loop of `SSTORE`/`SLOAD` or `TSTORE`/`TLOAD` on the colliding keys causes each cache access to degrade to a linear bucket scan, so `n` operations cost O(n²) CPU instead of O(n), while energy is still charged at the flat per-op rate (`SLOAD`=50, `TLOAD`/`TSTORE`=100): [8](#0-7) [9](#0-8) 

### Impact Explanation
Because energy accounting charges a fixed cost per `SLOAD`/`SSTORE`/`TLOAD`/`TSTORE` regardless of actual cache-bucket-walk cost, the attacker pays for O(n) work but the node performs O(n²) work reprocessing every node that validates/executes the transaction (and every full node re-executing the block). With a sufficiently large but still energy-affordable operation count, this can materially slow down or stall transaction/block processing on every node in the network for a single crafted transaction — a network-wide denial-of-service reachable purely from an unprivileged, unauthenticated contract deployer/caller with no special privileges, satisfying the "node crash or halt" / "API the node can no longer serve" bar.

### Likelihood Explanation
Grinding base-31-hash collisions for 32-byte keys is a well-known, computationally cheap offline technique (same construction as historical Java `HashMap`/`HashSet` collision DoS attacks); an attacker only needs to precompute colliding `DataWord`/`Key` values once and embed them as `PUSH` constants in the exploit contract's bytecode. No special TRON permissions, staking, or witness/SR status are required — any account with enough TRX/energy to deploy and call a contract can trigger it, making this highly likely to be exploitable by any anonymous transaction broadcaster.

### Recommendation
Replace the grindable, non-treeified `HashMap`/`HashSet`/`HashBasedTable` collections keyed by `DataWord`/`Key` in `Storage.rowCache` and `RepositoryImpl`'s per-transaction caches (especially `transientStorage`, `storageCache`, `newContractCache`, `selfDestructCache`) with `TreeMap`/`TreeSet`/`TreeBasedTable` using each key's natural byte-wise ordering (`DataWord` already implements `Comparable<DataWord>`; `Key` should be made `Comparable` as well), so insertion/lookup cost is bounded to O(log n) regardless of attacker-chosen key distribution — mirroring the fix applied in Besu PR #10895 (commit `adfa98d`).

### Proof of Concept
1. Offline, brute-force/grind a set of N (e.g., 50,000) distinct 32-byte values `k_0..k_{N-1}` such that `Arrays.hashCode(k_i)` (and thus the `HashMap` bucket index after JDK's `hash()` spreading) collides for a fixed target `HashMap` capacity — feasible in seconds/minutes given the simple 31-multiplier construction with no seed/salt.
2. Deploy a contract whose constructor or a callable function executes a tight loop of `SSTORE`/`SLOAD` (or, if Cancun/`allowTvmCancun` is enabled, `TSTORE`/`TLOAD`) using `k_0..k_{N-1}` as storage/transient-storage keys, e.g. bytecode equivalent to:
   ```
   for i in 0..N: PUSH32 k_i; PUSH1 0x01; SSTORE
   ```
3. Call this function with enough `feeLimit`/energy to cover N × 100 (TSTORE) or N × 20000 (fresh SSTORE) energy units, which is affordable since energy cost is linear while actual CPU cost of servicing the `HashMap`/`HashBasedTable` becomes quadratic due to bucket collisions in `Storage.rowCache` ( [10](#0-9) ) or `RepositoryImpl.transientStorage` ( [11](#0-10) ).
4. Measure that a single transaction with N colliding keys takes disproportionately longer (quadratic growth) to execute/validate compared to N non-colliding keys, confirming the O(n²) CPU blowup on every node that processes the transaction/block.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/program/Storage.java (L17-19)
```java
  private static final int PREFIX_BYTES = 16;
  @Getter
  private final Map<DataWord, StorageRowCapsule> rowCache = new HashMap<>();
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

**File:** common/src/main/java/org/tron/common/runtime/vm/DataWord.java (L478-481)
```java
  @Override
  public int hashCode() {
    return java.util.Arrays.hashCode(data);
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/repository/RepositoryImpl.java (L127-142)
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
```

**File:** actuator/src/main/java/org/tron/core/vm/repository/RepositoryImpl.java (L465-484)
```java
  public byte[] getTransientStorageValue(byte[] address, byte[] key) {
    Key cacheAddress = new Key(address);
    Key cacheKey = new Key(key);
    if (transientStorage.contains(cacheAddress, cacheKey)) {
      return transientStorage.get(cacheAddress, cacheKey).getValue();
    }

    byte[] value;
    if (parent != null) {
      value = parent.getTransientStorageValue(address, key);
    } else {
      value = null;
    }

    if (value != null) {
      transientStorage.put(cacheAddress, cacheKey, Value.create(value));
    }

    return value;
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/repository/RepositoryImpl.java (L656-659)
```java
  @Override
  public void updateTransientStorageValue(byte[] address, byte[] key, byte[] value) {
    transientStorage.put(Key.create(address), Key.create(key), Value.create(value, Type.DIRTY));
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/repository/Key.java (L50-53)
```java
  @Override
  public int hashCode() {
    return data != null ? ArrayUtils.hashCode(data) : 0;
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/OperationActions.java (L658-681)
```java
  public static void tLoadAction(Program program) {
    DataWord key = program.stackPop();
    DataWord address = program.getContractAddress();

    byte[] data =
        program.getContractState().getTransientStorageValue(address.getData(), key.getData());
    DataWord value = data != null ? new DataWord(data).clone() : DataWord.ZERO();

    program.stackPush(value);
    program.step();
  }

  public static void tStoreAction(Program program) {
    if (program.isStaticCall()) {
      throw new Program.StaticCallModificationException();
    }
    DataWord key = program.stackPop();
    DataWord value = program.stackPop();
    DataWord address = program.getContractAddress();

    program.getContractState()
        .updateTransientStorageValue(address.getData(), key.getData(), value.getData());
    program.step();
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/EnergyCost.java (L30-30)
```java
  private static final long SLOAD = 50;
```

**File:** actuator/src/main/java/org/tron/core/vm/EnergyCost.java (L61-62)
```java
  private static final long TLOAD = 100;
  private static final long TSTORE = 100;
```
