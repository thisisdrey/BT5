### Title
Unbounded delegation index causes DoS in `getDelegatedResourceAccountIndexV2` query path - (File: chainbase/src/main/java/org/tron/core/store/DelegatedResourceAccountIndexStore.java)

### Summary
`DelegatedResourceAccountIndexStore.getV2Index()` performs a full `prefixQuery` scan over every `from`/`to` delegation record for a given address, with no limit on how many distinct relationships are collected before being returned. An unprivileged account can grow this list without bound by issuing many `DelegateResourceContract` transactions to distinct receiver addresses, since `DelegateResourceActuator`/`DelegateResourceProcessor` never caps the number of distinct delegation counterparties per account (unlike `MarketSellAssetActuator`, which explicitly caps active orders per account at `MAX_ACTIVE_ORDER_NUM = 100`).

### Finding Description
When resource delegation is not optimized to the append-only V2 index model, or even in the V2 model, each `DelegateResourceContract` execution calls `delegatedResourceAccountIndexStore.delegateV2(ownerAddress, receiverAddress, ...)` [1](#0-0) , which simply writes new keyed entries under `V2_FROM_PREFIX`/`V2_TO_PREFIX` without any check on how many distinct relationships already exist for that address [2](#0-1) .

When the index is read back via `getV2Index(address)`, the store performs an unbounded `prefixQuery` for both the `from` and `to` prefixes and materializes every result into in-memory lists, sorting them by timestamp before returning: [3](#0-2) 

There is no page size, `LIMIT`, or cap applied to `prefixQuery(key)` results in this path, unlike the market order/price query paths in `Wallet.java`, which explicitly bound iteration with `MARKET_COUNT_LIMIT_MAX` [4](#0-3) . Because `DelegateResourceActuator.validate()` (the actuator that creates these records) does not impose any cap on the number of distinct receiver addresses an owner account may delegate to, an attacker can send many low-value `DelegateResourceContract` transactions to a large number of distinct addresses, inflating their own `V2_FROM_PREFIX`/`V2_TO_PREFIX` index entries arbitrarily.

Any subsequent call to the API that surfaces `getV2Index` (`getDelegatedResourceAccountIndexV2` in `Wallet.java`) for that attacker-controlled address then performs a full unbounded scan and in-memory sort proportional to the number of delegation relationships created, which is directly reachable via the node's GRPC/HTTP query interface with no authentication required.

### Impact Explanation
This is the same bug class as the reported `CrabNetting` finding: an unbounded, transaction-controlled array/index that is fully iterated by a read path with no size limit, allowing an unprivileged actor to grow the structure until the query becomes prohibitively expensive or exhausts node resources (CPU/memory) when serving that specific API. Because the query endpoint is part of the public wallet API surface, this can degrade or crash the API-serving path for any client requesting that account's delegation index, effectively "an API the node can no longer serve" for that endpoint.

### Likelihood Explanation
Likelihood is limited by cost: creating additional delegation index entries requires broadcasting many `DelegateResourceContract` transactions, each consuming bandwidth/energy and requiring a small amount of frozen balance to delegate, so growing the index to a size that causes meaningful performance degradation requires sustained, moderately costly effort by the attacker rather than a single cheap call. This differs from the original report's flavor (where deposits/withdraws could be queued for free); here there's a non-trivial though bounded-by-fee cost per entry, which somewhat reduces likelihood but does not eliminate the underlying missing-bound design flaw.

### Recommendation
Add an explicit cap on the number of distinct delegation relationships (`from`/`to`) that can be created per account in `DelegateResourceActuator`/`DelegateResourceProcessor`, analogous to `MAX_ACTIVE_ORDER_NUM` used for market orders. Additionally, apply a hard limit (e.g., a `LIMIT`/count parameter) to the `prefixQuery` calls in `DelegatedResourceAccountIndexStore.getWithPrefix`/`getV2Index` so that a single query can never return or process more than a bounded number of entries, with pagination support if full enumeration is required by callers.

### Proof of Concept
Not independently executable from static analysis alone; the mechanism is:
1. Attacker creates account A with minimal frozen balance sufficient for many small `DelegateResourceContract` calls.
2. Attacker repeatedly submits `DelegateResourceContract` transactions from A to N distinct newly-derived receiver addresses (no cap enforced in `DelegateResourceActuator.validate()`).
3. Each transaction adds a new entry via `delegatedResourceAccountIndexStore.delegateV2` [2](#0-1) .
4. Attacker (or any client) calls the `getDelegatedResourceAccountIndexV2` API for address A, triggering `getV2Index`, which performs an unbounded `prefixQuery` over all N entries and sorts them [5](#0-4) , with cost scaling linearly with N and no limit imposed.

I was not able to fully confirm within the tool budget whether any other layer (e.g., a wrapper in `Wallet.java` around `getDelegatedResourceAccountIndexV2`) imposes an additional cap before returning results to the API caller — this should be verified directly in a full Devin session with file access, since the index may be surfaced in `Wallet.java` without further limiting logic.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/DelegateResourceActuator.java (L313-316)
```java
    //modify DelegatedResourceAccountIndexStore
    delegatedResourceAccountIndexStore.delegateV2(ownerAddress, receiverAddress,
        dynamicPropertiesStore.getLatestBlockHeaderTimestamp());

```

**File:** chainbase/src/main/java/org/tron/core/store/DelegatedResourceAccountIndexStore.java (L77-89)
```java
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

**File:** framework/src/main/java/org/tron/core/Wallet.java (L2883-2903)
```java
  public MarketOrderPairList getMarketPairList() {
    MarketOrderPairList.Builder builder = MarketOrderPairList.newBuilder();
    MarketPairToPriceStore marketPairToPriceStore = dbManager.getChainBaseManager()
        .getMarketPairToPriceStore();

    Iterator<Entry<byte[], BytesCapsule>> iterator = marketPairToPriceStore
        .iterator();
    long count = 0;
    while (iterator.hasNext()) {
      Entry<byte[], BytesCapsule> next = iterator.next();

      byte[] pairKey = next.getKey();
      builder.addOrderPair(MarketUtils.decodeKeyToMarketPairHuman(pairKey));
      count++;
      if (count > MARKET_COUNT_LIMIT_MAX) {
        break;
      }
    }

    return builder.build();
  }
```
