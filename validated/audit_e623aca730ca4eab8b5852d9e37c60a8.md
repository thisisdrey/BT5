### Title
Algorithmic-Complexity DoS via Unkeyed Hash Collisions in the In-Memory Snapshot Layer (`WrappedByteArray`/`Storage.rowCache`) - (File: `chainbase/src/main/java/org/tron/core/db2/common/WrappedByteArray.java`)

### Summary
The Tor CVE stems from hashing attacker-chosen circuit-ID data with a non-resistant algorithm, letting a remote peer force expensive hash-bucket collisions. The closest reachable analog in java-tron is the state-layer key wrapper `WrappedByteArray`, used as the `HashMap` key type for every uncommitted store write (accounts, contracts, storage rows, assets) in the revoking/snapshot cache, and the VM's per-call `Storage.rowCache` keyed by `DataWord`. Both use Java's default `Arrays.hashCode(bytes)` — a fixed, unsalted 31-multiplier polynomial hash — and neither key type is protected by Java `HashMap`'s Java-8 bin-to-tree degeneration guard in the `WrappedByteArray` case (it does not implement `Comparable`), so an attacker who can choose the underlying bytes (e.g., 32-byte `SSTORE` storage keys, freely chosen by contract bytecode) can precompute large sets of colliding keys offline and force them into the map, degrading lookups from O(1) to O(n).

### Finding Description
`WrappedByteArray` is the key/value wrapper used throughout the `Chainbase`/`SnapshotImpl` in-memory diff layer that backs every `TronStoreWithRevoking`-based store (accounts, contracts, storage rows, assets, delegated resources, etc.): [1](#0-0) 

Its `hashCode()`/`equals()` are the plain `Arrays.hashCode`/`Arrays.equals` over the raw key bytes — deterministic, unsalted, and identical to the algorithm underlying the well-known 2011-era "hash flooding" class of algorithmic-complexity DoS attacks (the same bug class described in the report: "an attacker-chosen ... ID [used] to cause algorithm inefficiency" via mishandled hashing). Because `WrappedByteArray` does not implement `Comparable`, it cannot benefit from `java.util.HashMap`'s Java-8 mitigation that converts an overloaded bucket into a red-black tree (that optimization requires the key class to be `Comparable`); a bucket with many colliding `WrappedByteArray` keys stays a plain linked list, so lookups/inserts degrade linearly with the number of colliding entries.

The most directly attacker-controlled path to feed this structure is TVM storage: SSTORE/SLOAD keys are arbitrary 32-byte values fully chosen by contract bytecode. `Storage` (used by every contract call) caches rows in a `HashMap<DataWord, StorageRowCapsule>`: [2](#0-1) [3](#0-2) 

`DataWord.hashCode()` is likewise the plain `Arrays.hashCode(data)`: [4](#0-3) 

Each composed row key is subsequently written through `StorageRowStore` into the revoking/snapshot store, whose committed diffs are held in `WrappedByteArray`-keyed maps (`SnapshotImpl`, `Chainbase`), so contract-chosen storage keys with precomputed hash-bucket collisions propagate directly into the vulnerable structure.

### Impact Explanation
An attacker who deploys or calls a contract that performs many `SSTORE`s at keys precomputed (offline, since the hash function is public and unsalted) to collide into the same `HashMap` bucket can force every subsequent lookup/insert against that bucket to become O(n) rather than O(1). Because the diff layer accumulates across every uncommitted session/block until a flush, and the same non-resistant hash is reused at every store type sharing this key wrapper, the degraded performance compounds validation/execution time on all full nodes processing the same transactions — a CPU-exhaustion denial-of-service reachable purely by broadcasting ordinary signed transactions/contract calls, matching the report's "algorithm inefficiency via attacker-chosen ... ID" bug class.

### Likelihood Explanation
Moderate. Building enough colliding keys requires only offline computation against a public, unsalted hash function (no cryptographic secret is needed, unlike keyed hashing schemes designed to resist this exact class of attack), and `SSTORE` keys are fully attacker-chosen at negligible extra cost beyond normal energy fees for storage writes. However, per-transaction energy metering bounds how many distinct keys a single transaction can touch, so a meaningful degradation likely requires sustained submission of many such transactions/contracts rather than a single request — this somewhat limits the "single transaction" severity relative to the Tor analog, where a single crafted cell triggers the issue.

### Recommendation
Replace the raw `Arrays.hashCode`/`Arrays.equals` based key hashing in `WrappedByteArray` (and `DataWord` where used as a hash-map key in hot paths like `Storage.rowCache`) with a hashing strategy resistant to precomputed collisions (e.g., a per-process randomized seed mixed into the hash, or use a data structure keyed by a structure that supports `Comparable`/tree-based buckets, such as `TreeMap` or ensuring the key type implements `Comparable` so `HashMap`'s treeification kicks in). Alternatively, bound the number of distinct storage keys/state keys a single transaction or block can introduce more tightly, and monitor/limit per-bucket occupancy.

### Proof of Concept
1. Offline, using the known `Arrays.hashCode` polynomial algorithm, compute a large set of 32-byte values `k_1..k_m` that collide into the same internal `HashMap` bucket index for the expected table size used by `Storage.rowCache`/`WrappedByteArray`-backed snapshot maps.
2. Deploy a contract whose fallback/entry function performs `SSTORE(k_i, v_i)` for as many `k_i` as the energy limit allows per call.
3. Repeatedly invoke this contract across multiple transactions/blocks so the colliding keys accumulate in the store's revoking/snapshot diff layer.
4. Observe increasing latency in state read/write and block-processing time on full nodes as `HashMap` bucket lookups degrade from O(1) toward O(n) with the growing collision chain, compared to an equal number of non-colliding keys.

### Citations

**File:** chainbase/src/main/java/org/tron/core/db2/common/WrappedByteArray.java (L39-42)
```java
  @Override
  public int hashCode() {
    return Arrays.hashCode(bytes);
  }
```

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
