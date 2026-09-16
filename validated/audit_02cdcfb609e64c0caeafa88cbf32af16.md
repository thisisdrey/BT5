### Title
Null pointer dereference in `StorageRowStore.get()` on missing/unparseable storage row - (File: chainbase/src/main/java/org/tron/core/store/StorageRowStore.java)

### Summary
`StorageRowStore.get()` unconditionally dereferences the result of `getUnchecked(key)` without a null check, mirroring the Firebird `op_slice` root cause: an "unprepared" (potentially null) structure is handed directly to a method that assumes it is non-null, causing a `NullPointerException`. This store backs TVM contract storage reads (`SLOAD`), so the code path is reachable from any smart-contract execution triggered by an unauthenticated `TriggerSmartContract`/`eth_call` request.

### Finding Description
`StorageRowStore.get()` is: [1](#0-0) 

It calls `getUnchecked(key)` and immediately calls `row.setRowKey(key)` with no null check. `TronStoreWithRevoking.getUnchecked()` can legitimately return `null`: it wraps the raw DB bytes with `of(value)` and swallows any `BadItemException` (thrown when the reflective `StorageRowCapsule(byte[])` constructor invocation fails) by returning `null`: [2](#0-1) 

This `get()` is the storage-row accessor used by the TVM's `Storage` abstraction, which reads/writes every contract storage slot: [3](#0-2) 

`Storage.getValue()` does guard against a null `row`/`row.getInstance()` returned from `store.get(...)`, but that guard is evaluated *after* `StorageRowStore.get()` has already dereferenced a potentially-null `row` internally via `row.setRowKey(key)` — the NPE happens inside `get()` itself, before the caller's null check can take effect. This is architecturally identical to the Firebird bug class: a function receives a structure that may be null/uninitialized and dereferences it without validation.

The call chain from an attacker-controlled trigger down to this method is:
`RepositoryImpl.getStorageValue()` → `Storage.getValue()` → `StorageRowStore.get()`, invoked from the `SLOAD` opcode handler: [4](#0-3) [5](#0-4) 

### Impact Explanation
An uncaught `NullPointerException` thrown deep inside store access code, outside the actuator's normal `ContractValidateException`/`ContractExeException` handling, can propagate up through TVM execution and crash or destabilize the block-processing thread (or the JSON-RPC/HTTP query thread for read-only `eth_call`/`triggercontract` requests), matching the "node crash / API the node can no longer serve" bar. This qualifies as Medium/High depending on exact exception propagation and thread isolation, consistent with the referenced CVE's High severity for a comparable null-structure dereference during packet/request processing.

### Likelihood Explanation
Triggering the underlying `BadItemException` requires stored row bytes that fail to parse into a `StorageRowCapsule` (e.g., corrupted or legacy-format bytes for a given key), which is a narrower condition than a purely network-triggerable bug — I could not fully verify from the available index whether `StorageRowCapsule`'s byte-array constructor can throw on inputs reachable in normal (non-corrupted) operation. Because of this, I could not conclusively confirm an attacker-only path to invoke `BadItemException` without index access to `StorageRowCapsule.java`, which was not returned by search. Given the index size limits noted in the coverage policy, I recommend starting a Devin session to inspect `chainbase/src/main/java/org/tron/core/capsule/StorageRowCapsule.java` directly to confirm whether malformed/short byte arrays (which could occur from state corruption, migration edge cases, or a resized value under the same key across DB versions) cause the reflective constructor call in `of()` to throw, which is the precondition for this null return and the NPE crash.

### Recommendation
Add a null check in `StorageRowStore.get()` before dereferencing `row`, returning `null` (or constructing a fresh empty `StorageRowCapsule` bound to `key`) when `getUnchecked(key)` returns `null`, so that callers like `Storage.getValue()` receive a consistent contract instead of an uncaught NPE:
```java
@Override
public StorageRowCapsule get(byte[] key) {
  StorageRowCapsule row = getUnchecked(key);
  if (row == null) {
    return null;
  }
  row.setRowKey(key);
  return row;
}
```

### Proof of Concept
Not independently verified end-to-end due to inability to confirm from the indexed code whether `StorageRowCapsule`'s constructor can throw on the value bytes actually stored for a given key (which is required to make `getUnchecked` return `null`). Conceptually: an attacker deploys/calls a contract whose bytecode issues `SLOAD` against a storage key whose underlying DB row cannot be parsed by `StorageRowCapsule(byte[])` (e.g., due to a corrupted/incompatible on-disk value), triggering `BadItemException` → `getUnchecked` returns `null` → `StorageRowStore.get()` calls `null.setRowKey(key)` → `NullPointerException` uncaught in the SLOAD opcode handling path. Confirming the exact byte-level trigger requires reading `StorageRowCapsule.java`, which was not available in the current index; a Devin session with full repository access would be needed to validate this precondition and reproduce the crash.

### Citations

**File:** chainbase/src/main/java/org/tron/core/store/StorageRowStore.java (L19-24)
```java
  @Override
  public StorageRowCapsule get(byte[] key) {
    StorageRowCapsule row = getUnchecked(key);
    row.setRowKey(key);
    return row;
  }
```

**File:** chainbase/src/main/java/org/tron/core/db/TronStoreWithRevoking.java (L107-133)
```java
  @Override
  public T getUnchecked(byte[] key) {
    byte[] value = revokingDB.getUnchecked(key);

    try {
      return of(value);
    } catch (BadItemException e) {
      return null;
    }
  }

  @Override
  public T getFromRoot(byte[] key) throws ItemNotFoundException, BadItemException{
    return of(revokingDB.getFromRoot(key)) ;

  }

  public T of(byte[] value) throws BadItemException {
    try {
      Constructor constructor = token.getRawType().getConstructor(byte[].class);
      @SuppressWarnings("unchecked")
      T t = (T) constructor.newInstance(value);
      return t;
    } catch (NoSuchMethodException | IllegalAccessException | InstantiationException | InvocationTargetException e) {
      throw new BadItemException(e.getMessage());
    }
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

**File:** actuator/src/main/java/org/tron/core/vm/repository/RepositoryImpl.java (L704-708)
```java
  @Override
  public DataWord getStorageValue(byte[] address, DataWord key) {
    Storage storage = getStorageInternal(address);
    return storage == null ? null : storage.getValue(key);
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/OperationActions.java (L588-598)
```java
  public static void sLoadAction(Program program) {
    DataWord key = program.stackPop();
    DataWord val = program.storageLoad(key);

    if (val == null) {
      val = key.and(DataWord.ZERO);
    }

    program.stackPush(val);
    program.step();
  }
```
