Confirmed root cause chain: a storage word read via `SLOAD` is wrapped in a `DataWord` that shares its backing array with the persistent cache entry, and several `DataWord` arithmetic opcodes mutate that backing array **in place**, corrupting the cached/committed storage value — directly analogous to the kernel bug where a buffer that looks privately owned is actually shared and gets mutated in place.

### Title
Shared storage-row buffer mutated in place via `DataWord` bitwise ops corrupts committed contract storage - (File: actuator/src/main/java/org/tron/core/vm/program/Storage.java)

### Summary
`Storage.getValue(DataWord key)` returns `new DataWord(row.getValue())` where `row` is the cached `StorageRowCapsule` for that slot [1](#0-0) . `StorageRowCapsule.getValue()`/`getInstance()` return the internal `rowValue` array **by reference**, never cloned [2](#0-1) . `DataWord`'s constructor, when given a 32-byte array, stores it **by reference** instead of copying it: `else if (data.length == WORD_SIZE) { this.data = data; }` [3](#0-2) . This "fresh" `DataWord` pushed by `SLOAD` therefore still aliases the exact same byte array owned by the row cache (and, if the row is dirty, the array that will be persisted to the state store).

Several `DataWord` operators mutate `this.data` in place rather than allocating a new array, e.g. `xor()`:
```
for (int i = 0; i < this.data.length; ++i) {
  this.data[i] ^= w2.data[i];
}
``` [4](#0-3) 

### Finding Description
`Storage` maintains `rowCache: Map<DataWord, StorageRowCapsule>` for the lifetime of a contract execution context, reusing the same `StorageRowCapsule`/`byte[]` across multiple `SLOAD`s to the same key within a transaction or nested call [5](#0-4) . When bytecode executes `SLOAD` and then applies an in-place bitwise opcode (e.g. `XOR`, or `NOT`/`bnot()` when the value is zero) directly on the loaded word, the mutation is applied to the array object still referenced by the cached `StorageRowCapsule.rowValue`, not to a private copy. Because `DataWord`'s 32-byte constructor path skips the defensive copy that `getClonedData()` performs elsewhere in the same class [6](#0-5) , code throughout the VM that treats a freshly constructed `DataWord` as an independently-owned 32-byte buffer is actually operating on shared, cache-owned (and potentially store-bound) memory — exactly the "in-place mutation of a not-privately-owned buffer" bug class described in the analog report.

`Storage.commit()` persists any row still marked `dirty` using `row.getValue()` — the same aliased array — to the `StorageRowStore` [7](#0-6) . If a slot was written via `SSTORE` earlier in the same call (marking it dirty) and later read back with `SLOAD` and mutated via an in-place opcode, the corrupted bytes get committed as the slot's on-chain value; if the slot was clean, subsequent `SLOAD`s within the same call/nested call return the corrupted value from cache even though no `SSTORE` occurred, silently diverging contract-visible storage from what was intended.

### Impact Explanation
A single unprivileged contract deployer can craft bytecode whose `SLOAD`→bitwise-op sequence corrupts its own persistent storage slots (e.g., an ERC20-style balance/allowance mapping slot), producing an unbacked or incorrectly zeroed/altered balance entry that gets committed to the chain state without an explicit, semantically-correct `SSTORE`. This is a state-integrity violation reachable purely by deploying and calling a contract — no privileged role required — and can lead to permanent corruption or freezing of stored value for that contract.

### Likelihood Explanation
Reaching the vulnerable path requires only standard, unprivileged actions: deploy a contract and invoke a function whose EVM/TVM bytecode contains `SLOAD` immediately followed by an opcode that mutates the resulting `DataWord` in place (`XOR` is the clearest built-in that does `this.data[i] ^= ...`) [4](#0-3) . Such bytecode sequences (e.g. from Solidity patterns using `^=` on a storage-loaded value, or hand-crafted opcodes) are trivial to construct and require no special permissions, elevated energy, or race conditions.

### Recommendation
Make `Storage.getValue()` return a `DataWord` backed by a defensive copy (use `DataWord`'s clone/`getClonedData()`-style copy instead of the raw array), or change the `DataWord(byte[])` 32-byte fast path to always copy the input array so that no external mutation can alias cache/store-owned memory, mirroring how `Key.getBytes()` and `WrappedByteArray.copyOf()` already defensively copy in this codebase [8](#0-7) .

### Proof of Concept
1. Deploy a contract with a storage slot `S` initialized via `SSTORE` in the same execution frame (marks the row dirty in `Storage.rowCache`).
2. In the same call, execute bytecode equivalent to: `PUSH <S>`, `SLOAD` (pushes a `DataWord` that aliases the cached `StorageRowCapsule.rowValue` array), `PUSH <mask>`, `XOR` (mutates the aliased array in place via `DataWord.xor()`).
3. Do not `SSTORE` the XOR result back to `S`.
4. On `Storage.commit()`, because the row is still marked dirty from step 1, the corrupted (XORed) array — not the value intended by the original `SSTORE` — is written to `StorageRowStore`, permanently altering the contract's committed storage for slot `S` without an explicit write to that effect.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/program/Storage.java (L15-23)
```java
public class Storage {

  private static final int PREFIX_BYTES = 16;
  @Getter
  private final Map<DataWord, StorageRowCapsule> rowCache = new HashMap<>();
  @Getter
  private byte[] addrHash;
  @Getter
  private StorageRowStore store;
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

**File:** chainbase/src/main/java/org/tron/core/capsule/StorageRowCapsule.java (L63-80)
```java
  public byte[] getValue() {
    return this.rowValue;
  }

  public void setValue(byte[] value) {
    this.rowValue = value;
    markDirty();
  }

  @Override
  public byte[] getData() {
    return this.rowValue;
  }

  @Override
  public byte[] getInstance() {
    return this.rowValue;
  }
```

**File:** common/src/main/java/org/tron/common/runtime/vm/DataWord.java (L83-93)
```java
  public DataWord(byte[] data) {
    if (data == null) {
      this.data = ByteUtil.EMPTY_BYTE_ARRAY;
    } else if (data.length == WORD_SIZE) {
      this.data = data;
    } else if (data.length < WORD_SIZE) {
      System.arraycopy(data, 0, this.data, WORD_SIZE - data.length, data.length);
    } else {
      throw new RuntimeException("Data word can't exceed 32 bytes: " + ByteArray.toHexString(data));
    }
  }
```

**File:** common/src/main/java/org/tron/common/runtime/vm/DataWord.java (L162-177)
```java
  public byte[] getData() {
    return data;
  }

  /**
   * be careful, this one will not throw Exception when data.length > WORD_SIZE
   */
  public byte[] getClonedData() {
    byte[] ret = ByteUtil.EMPTY_BYTE_ARRAY;
    if (data != null) {
      ret = new byte[WORD_SIZE];
      int dataSize = min(data.length, WORD_SIZE, VMConfig.disableJavaLangMath());
      System.arraycopy(data, 0, ret, 0, dataSize);
    }
    return ret;
  }
```

**File:** common/src/main/java/org/tron/common/runtime/vm/DataWord.java (L301-307)
```java
  public DataWord xor(DataWord w2) {

    for (int i = 0; i < this.data.length; ++i) {
      this.data[i] ^= w2.data[i];
    }
    return this;
  }
```

**File:** chainbase/src/main/java/org/tron/core/db2/common/Key.java (L23-30)
```java
  public byte[] getBytes() {
    byte[] key = data.getBytes();
    if (key == null) {
      return null;
    }

    return Arrays.copyOf(key, key.length);
  }
```
