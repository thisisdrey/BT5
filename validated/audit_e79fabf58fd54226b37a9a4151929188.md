### Title
Unbounded per-account delegation index growth causes gas/CPU exhaustion and unusable query API - ([File: chainbase/src/main/java/org/tron/core/store/DelegatedResourceAccountIndexStore.java])

### Summary
The reported ERC-1155 bug is an unbounded per-`(account, tokenId)` array that any attacker can grow via cheap, repeated calls, later making the victim's own `balanceOf`/`transfer` calls fail from gas exhaustion. The closest reachable analog in java-tron is the delegated-resource bookkeeping keyed by receiver address: any unprivileged account can call `DelegateResourceContract` targeting a fixed victim receiver, and each such call from a distinct sender permanently adds an entry associated with the victim, with no cap on the number of entries. Later, reads/writes over that victim-keyed data structure become correspondingly more expensive.

### Finding Description
Two related growth points exist:

1. **Legacy (non-optimized) index model** — `DelegatedResourceAccountIndexCapsule.addToAccount`/`addFromAccount` unconditionally append to `toAccounts`/`fromAccounts` repeated fields with no size limit: [1](#0-0) 
This is driven from `FreezeBalanceActuator.delegateResource`, reachable directly by any signed `FreezeBalanceContract` transaction with an arbitrary `receiverAddress` (the victim), when `!dynamicPropertiesStore.supportAllowDelegateOptimization()`: [2](#0-1) 
Each new distinct `(owner, receiver)` pair adds one more entry to the victim's `fromAccountsList` (a list that never shrinks except on full un-delegation of that specific pair). Later un-delegation performs an `O(n)` linear `List.remove` scan of that list: [3](#0-2) 

2. **Current (V2/optimized) index model** — the equivalent list is *not* stored as a bounded protobuf field but reconstructed on every query by an **unbounded** LevelDB/RocksDB prefix scan over all `(V2_FROM_PREFIX|V2_TO_PREFIX)+address` keys, sorted and materialized into a full in-memory list with no limit or pagination: [4](#0-3) 
Each `DelegateResourceContract` call (via `DelegateResourceProcessor`/`DelegateResourceActuator`) writes one new immutable key per unique `(from,to)` pair with no cap: [5](#0-4) 
This data is served in full through the `getDelegatedResourceAccountIndexV2` Wallet API path, exposed via HTTP/gRPC (`GetDelegatedResourceAccountIndexV2Servlet`, `RpcApiService`), and via the Solidity/PBFT read-only mirrors of the same code path.

### Impact Explanation
An attacker who funds many distinct TRON accounts (each needs only the minimum freeze/delegate amount) can repeatedly call `DelegateResourceContract`/`FreezeBalanceContract` targeting one victim address as `receiverAddress`. Every call permanently adds one more entry keyed to the victim, with:
- No maximum count check anywhere in `DelegateResourceActuator`/`FreezeBalanceActuator` validation.
- No pagination/limit in `getWithPrefix`/`prefixQuery`, so subsequent `getDelegatedResourceAccountIndex(V2)` queries for the victim must scan, sort, and serialize the entire (attacker-inflated) key set on every call.
- `O(n)` linear scans (`contains`/`remove`) in the legacy model that get progressively more expensive as the list grows, degrading the performance of the victim's own future freeze/un-freeze/un-delegate transactions.

At sufficient scale this can make the `getDelegatedResourceAccountIndex(V2)` API endpoint effectively unusable for the targeted address (excessive CPU/latency per request, possibly large response payloads), and can materially raise the cost/CPU time of the victim's own resource management transactions — matching "an API the node can no longer serve" from the validation criteria.

### Likelihood Explanation
Likelihood is moderate-to-low compared to the original report because, unlike the Solidity `Group[]` case (where a single attacker account can cheaply spam entries against one victim via ordinary gas), growing this java-tron structure requires **funding many distinct sending accounts** (sybil cost proportional to entries, each needing the minimum freeze/delegate amount), since a repeat call from the same sender to the same receiver does not add a new entry (deduplicated by key/`contains` check). This raises the attacker's cost relative to the report's scenario, but remains feasible for a moderately funded attacker and requires no special privilege — any transaction broadcaster can perform it.

### Recommendation
- **Short term:** Add a maximum count guard on the number of distinct delegators/delegatees tracked per address (both in the legacy `toAccounts`/`fromAccounts` fields and in the V2 prefix-key space), and add mandatory `limit`/`offset` (pagination) parameters to `getDelegatedResourceAccountIndex`/`getDelegatedResourceAccountIndexV2` and their HTTP/gRPC handlers so a single query cannot force an unbounded scan/response.
- **Long term:** Replace the `O(n)` list operations (`contains`, `remove`) in `DelegatedResourceAccountIndexCapsule` with an indexed/keyed storage scheme (e.g., store existence as a direct key lookup rather than scanning an embedded list), and bound the LevelDB/RocksDB prefix scan in `DelegatedResourceAccountIndexStore.getWithPrefix` with a hard cap plus streaming/pagination semantics.

### Proof of Concept
Conceptual (cannot be executed here, but derivable from the cited code):
1. Attacker creates `N` distinct funded TRON accounts (`A1..AN`), each with the minimum balance required to freeze and delegate resources.
2. From each `Ai`, attacker broadcasts a `DelegateResourceContract` (or legacy `FreezeBalanceContract` with delegation) transaction with `receiverAddress = Victim`.
3. Each transaction is independently valid (no per-receiver cap exists in `DelegateResourceActuator`/`FreezeBalanceActuator` validate methods) and adds one more `V2_TO_PREFIX+Victim+Ai` key (or, in legacy mode, one more entry in `Victim`'s `fromAccountsList`).
4. After `N` is large, any client calls `getDelegatedResourceAccountIndexV2(Victim)` via HTTP/gRPC; the node must execute `getWithPrefix`, performing a full prefix scan + sort + protobuf list construction over all `N` entries on every call, and (in the legacy model) the victim's own subsequent `UnfreezeBalanceActuator`/`FreezeBalanceActuator` calls perform `O(N)` `List.remove`/`contains` scans, degrading with `N`.

**Uncertainty:** I could not fully confirm the exact production-relevant value of `supportAllowDelegateOptimization` (i.e., whether the legacy code path in `FreezeBalanceActuator`/`UnfreezeBalanceActuator` is still reachable on current mainnet configuration) or measure the concrete CPU/latency cost of `getWithPrefix` at scale, since I do not have execution/benchmarking access. The V2 prefix-scan mechanism is confirmed unbounded, but the real-world severity depends on production defaults and Java/RocksDB prefix-scan performance characteristics not fully explorable via static code search alone.

### Citations

**File:** chainbase/src/main/java/org/tron/core/capsule/DelegatedResourceAccountIndexCapsule.java (L57-61)
```java
  public void addFromAccount(ByteString fromAccount) {
    this.delegatedResourceAccountIndex = this.delegatedResourceAccountIndex.toBuilder()
        .addFromAccounts(fromAccount)
        .build();
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/FreezeBalanceActuator.java (L320-345)
```java
    if (!dynamicPropertiesStore.supportAllowDelegateOptimization()) {

      DelegatedResourceAccountIndexCapsule ownerIndexCapsule =
          delegatedResourceAccountIndexStore.get(ownerAddress);
      if (ownerIndexCapsule == null) {
        ownerIndexCapsule = new DelegatedResourceAccountIndexCapsule(
            ByteString.copyFrom(ownerAddress));
      }
      List<ByteString> toAccountsList = ownerIndexCapsule.getToAccountsList();
      if (!toAccountsList.contains(ByteString.copyFrom(receiverAddress))) {
        ownerIndexCapsule.addToAccount(ByteString.copyFrom(receiverAddress));
      }
      delegatedResourceAccountIndexStore.put(ownerAddress, ownerIndexCapsule);

      DelegatedResourceAccountIndexCapsule receiverIndexCapsule
          = delegatedResourceAccountIndexStore.get(receiverAddress);
      if (receiverIndexCapsule == null) {
        receiverIndexCapsule = new DelegatedResourceAccountIndexCapsule(
            ByteString.copyFrom(receiverAddress));
      }
      List<ByteString> fromAccountsList = receiverIndexCapsule
          .getFromAccountsList();
      if (!fromAccountsList.contains(ByteString.copyFrom(ownerAddress))) {
        receiverIndexCapsule.addFromAccount(ByteString.copyFrom(ownerAddress));
      }
      delegatedResourceAccountIndexStore.put(receiverAddress, receiverIndexCapsule);
```

**File:** actuator/src/main/java/org/tron/core/actuator/UnfreezeBalanceActuator.java (L163-182)
```java
        if (!dynamicStore.supportAllowDelegateOptimization()) {
          DelegatedResourceAccountIndexCapsule ownerIndexCapsule =
              delegatedResourceAccountIndexStore.get(ownerAddress);
          if (ownerIndexCapsule != null) {
            List<ByteString> toAccountsList = new ArrayList<>(ownerIndexCapsule
                .getToAccountsList());
            toAccountsList.remove(ByteString.copyFrom(receiverAddress));
            ownerIndexCapsule.setAllToAccounts(toAccountsList);
            delegatedResourceAccountIndexStore.put(ownerAddress, ownerIndexCapsule);
          }

          DelegatedResourceAccountIndexCapsule receiverIndexCapsule =
              delegatedResourceAccountIndexStore.get(receiverAddress);
          if (receiverIndexCapsule != null) {
            List<ByteString> fromAccountsList = new ArrayList<>(receiverIndexCapsule
                .getFromAccountsList());
            fromAccountsList.remove(ByteString.copyFrom(ownerAddress));
            receiverIndexCapsule.setAllFromAccounts(fromAccountsList);
            delegatedResourceAccountIndexStore.put(receiverAddress, receiverIndexCapsule);
          }
```

**File:** chainbase/src/main/java/org/tron/core/store/DelegatedResourceAccountIndexStore.java (L114-138)
```java
  public DelegatedResourceAccountIndexCapsule getV2Index(byte[] address) {
    return getWithPrefix(V2_FROM_PREFIX, V2_TO_PREFIX, address);
  }

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

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/DelegateResourceProcessor.java (L166-180)
```java
    //modify DelegatedResourceAccountIndex
    long now = repo.getDynamicPropertiesStore().getLatestBlockHeaderTimestamp();
    byte[] fromKey = Bytes.concat(
        DelegatedResourceAccountIndexStore.getV2_FROM_PREFIX(), ownerAddress, receiverAddress);
    DelegatedResourceAccountIndexCapsule toIndexCapsule =
        new DelegatedResourceAccountIndexCapsule(ByteString.copyFrom(receiverAddress));
    toIndexCapsule.setTimestamp(now);
    repo.updateDelegatedResourceAccountIndex(fromKey, toIndexCapsule);

    byte[] toKey = Bytes.concat(
        DelegatedResourceAccountIndexStore.getV2_TO_PREFIX(), receiverAddress, ownerAddress);
    DelegatedResourceAccountIndexCapsule fromIndexCapsule =
        new DelegatedResourceAccountIndexCapsule(ByteString.copyFrom(ownerAddress));
    fromIndexCapsule.setTimestamp(now);
    repo.updateDelegatedResourceAccountIndex(toKey, fromIndexCapsule);
```
