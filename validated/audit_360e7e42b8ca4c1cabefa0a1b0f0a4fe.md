### Title
Unbounded per-account delegation index growth causes O(n log n) cost on every `getDelegatedResourceAccountIndex`/`getDelegatedResourceAccountIndexV2` query - (File: `chainbase/src/main/java/org/tron/core/store/DelegatedResourceAccountIndexStore.java`)

### Summary
The bug class reported for YetiFinance (`poolColl.tokens[]` growing without bound and being fully scanned on every `getVC()` call, eventually causing out-of-gas/out-of-block-gas) has a direct structural analog in java-tron's delegated-resource indexing. Any account can cheaply cause its `DelegatedResourceAccountIndex` set (list of distinct delegation counterparties) to grow without any upper bound, and every subsequent index lookup re-scans and re-sorts the entire accumulated set.

### Finding Description
`DelegatedResourceAccountIndexStore.delegate()`/`delegateV2()` write one key per distinct `(from, to)` pair into the store with no limit on the number of distinct counterparties an account can accumulate: [1](#0-0) 

Reading the index (`getWithPrefix`, used by `getIndex`/`getV2Index`) performs a `prefixQuery` over all entries for the address, materializes them into a `List`, and then **sorts the entire list** by timestamp on every call, with no cap on list size: [2](#0-1) 

There is also a `convert()` path that iterates the full legacy `toAccountsList`/`fromAccountsList` and re-delegates every entry one by one, again with no size bound: [3](#0-2) 

An unprivileged account can trigger `delegate()` for a new, distinct receiver address on every `DelegateResourceContract` transaction (each delegation only needs a minimal amount of frozen resource, e.g. 1 TRX-equivalent), so the from/to index for that address grows by one entry per broadcast transaction with no protocol-level ceiling analogous to `poolColl.tokens[]`'s unlimited collateral list. This mirrors the exact root cause in the report: an attacker-controllable, monotonically growing array with a per-query full scan/sort and no length check.

### Impact Explanation
Because the index for an address is fully scanned and sorted on every read (`getWithPrefix`), and this is reachable through the public query surface (`Wallet.getDelegatedResourceAccountIndex`/`getDelegatedResourceAccountIndexV2`, exposed over gRPC/HTTP), an attacker can inflate a single address's index to a very large size using ordinary, cheap `DelegateResourceContract` transactions to many distinct receiver addresses. Subsequent queries for that address's index become increasingly expensive (CPU and memory for `prefixQuery` + `List` + `sort`), degrading or effectively denying that API path for legitimate callers — an "API the node can no longer serve" condition, which is an accepted impact class per this review's rules. The `convert()` legacy-migration path amplifies the same growable list by iterating and re-inserting every accumulated entry.

### Likelihood Explanation
Likelihood is high for the write-side growth: any account can call `DelegateResourceContract` repeatedly to distinct new receivers at low cost, and nothing in `DelegatedResourceAccountIndexStore.delegate()`/`delegateV2()` limits the number of distinct counterparties. The read-side cost scales with however large an attacker is willing to grow their own index, and it directly affects a public query API rather than requiring privileged access or SR/witness collusion.

### Recommendation
Bound the number of distinct delegation counterparties tracked per address (analogous to capping `poolColl.tokens[]`), or avoid materializing/sorting the full result set on every read — e.g., paginate `getWithPrefix`'s `prefixQuery` results, cache/maintain the sorted order incrementally instead of re-sorting on each call, and add a maximum count enforced at delegate-time (reject or require an explicit undelegate before adding new counterparties beyond a limit).

### Proof of Concept
1. From an unprivileged account `A`, broadcast a `DelegateResourceContract` to receiver `R1` for a minimal frozen amount; this calls `DelegatedResourceAccountIndexStore.delegate/delegateV2(A, R1, …)`.
2. Repeat with `R2, R3, …, Rn` (each a fresh distinct address) — the store persists one new key-pair entry per receiver, with no cap enforced in `delegate()`/`delegateV2()`.
3. Call `Wallet.getDelegatedResourceAccountIndex`/`V2` (or the corresponding HTTP/gRPC endpoint) for address `A`; `getWithPrefix` performs `prefixQuery` over all `n` entries and sorts them, with cost growing with `n` and no upper bound, degrading that query path as `n` increases.

### Citations

**File:** chainbase/src/main/java/org/tron/core/store/DelegatedResourceAccountIndexStore.java (L42-61)
```java
  public void convert(byte[] address) {
    DelegatedResourceAccountIndexCapsule indexCapsule = this.get(address);
    if (indexCapsule == null) {
      // convert complete or have no delegate
      return;
    }
    // convert old data
    List<ByteString> toList = indexCapsule.getToAccountsList();
    for (int i = 0; i < toList.size(); i++) {
      // use index as the timestamp, just to keep index in order
      this.delegate(address, toList.get(i).toByteArray(), i + 1L);
    }

    List<ByteString> fromList = indexCapsule.getFromAccountsList();
    for (int i = 0; i < fromList.size(); i++) {
      // use index as the timestamp, just to keep index in order
      this.delegate(fromList.get(i).toByteArray(), address, i + 1L);
    }
    this.delete(address);
  }
```

**File:** chainbase/src/main/java/org/tron/core/store/DelegatedResourceAccountIndexStore.java (L63-89)
```java
  public void delegate(byte[] from, byte[] to, long time) {
    byte[] fromKey = Bytes.concat(FROM_PREFIX, from, to);
    DelegatedResourceAccountIndexCapsule toIndexCapsule =
        new DelegatedResourceAccountIndexCapsule(ByteString.copyFrom(to));
    toIndexCapsule.setTimestamp(time);
    this.put(fromKey, toIndexCapsule);

    byte[] toKey = Bytes.concat(TO_PREFIX, to, from);
    DelegatedResourceAccountIndexCapsule fromIndexCapsule =
        new DelegatedResourceAccountIndexCapsule(ByteString.copyFrom(from));
    fromIndexCapsule.setTimestamp(time);
    this.put(toKey, fromIndexCapsule);
  }

  public void delegateV2(byte[] from, byte[] to, long time) {
    byte[] fromKey = Bytes.concat(V2_FROM_PREFIX, from, to);
    DelegatedResourceAccountIndexCapsule toIndexCapsule =
        new DelegatedResourceAccountIndexCapsule(ByteString.copyFrom(to));
    toIndexCapsule.setTimestamp(time);
    this.put(fromKey, toIndexCapsule);

    byte[] toKey = Bytes.concat(V2_TO_PREFIX, to, from);
    DelegatedResourceAccountIndexCapsule fromIndexCapsule =
        new DelegatedResourceAccountIndexCapsule(ByteString.copyFrom(from));
    fromIndexCapsule.setTimestamp(time);
    this.put(toKey, fromIndexCapsule);
  }
```

**File:** chainbase/src/main/java/org/tron/core/store/DelegatedResourceAccountIndexStore.java (L118-138)
```java
  private DelegatedResourceAccountIndexCapsule getWithPrefix(byte[] fromPrefix, byte[] toPrefix, byte[] address) {
    DelegatedResourceAccountIndexCapsule tmpIndexCapsule =
        new DelegatedResourceAccountIndexCapsule(ByteString.copyFrom(address));

    byte[] key = Bytes.concat(fromPrefix, address);
    List<DelegatedResourceAccountIndexCapsule> tmpToList =
        new ArrayList<>(this.prefixQuery(key).values());
    tmpToList.sort(Comparator.comparing(DelegatedResourceAccountIndexCapsule::getTimestamp));
    List<ByteString> list = tmpToList.stream()
        .map(DelegatedResourceAccountIndexCapsule::getAccount).collect(Collectors.toList());
    tmpIndexCapsule.setAllToAccounts(list);

    key = Bytes.concat(toPrefix, address);
    List<DelegatedResourceAccountIndexCapsule> tmpFromList =
        new ArrayList<>(this.prefixQuery(key).values());
    tmpFromList.sort(Comparator.comparing(DelegatedResourceAccountIndexCapsule::getTimestamp));
    list = tmpFromList.stream().map(DelegatedResourceAccountIndexCapsule::getAccount).collect(
        Collectors.toList());
    tmpIndexCapsule.setAllFromAccounts(list);
    return tmpIndexCapsule;
  }
```
