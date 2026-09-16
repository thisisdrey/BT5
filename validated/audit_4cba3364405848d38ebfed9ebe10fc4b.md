### Title
Unbounded Growth of `DelegatedResourceAccountIndex` Enables Denial-of-Service on Delegated-Resource Index Queries - (File: `chainbase/src/main/java/org/tron/core/store/DelegatedResourceAccountIndexStore.java`)

### Summary
Any account can call `DelegateResourceContract` (minimum cost: freezing/holding 1 TRX worth of bandwidth or energy) targeting an arbitrary receiver address. Each such call creates a brand-new, permanent key-value entry under a per-receiver key prefix in `DelegatedResourceAccountIndexStore`, with no limit on how many distinct delegator addresses (or delegator→receiver pairs) can point at the same victim account. A later read of that victim's delegated-resource index — exposed through the public gRPC/HTTP APIs — must scan and sort every entry under that prefix, which grows linearly (unbounded) with attacker effort. This mirrors the reported `NFTMarketAuction` pattern where an unbounded, cheaply-grown array (`bids[]`) is fully iterated by a later, unrelated operation (`getBestBid()`/`terminateAuction()`), causing excessive resource consumption/out-of-gas for whoever triggers the iteration.

### Finding Description
`DelegateResourceActuator.validate()`/`execute()` (and its TVM counterpart `DelegateResourceProcessor`) only check that the delegator has at least `TRX_PRECISION` (1 TRX) of available frozen V2 balance and that the receiver isn't a contract or the same address as the owner: [1](#0-0) [2](#0-1) 

Nothing limits the number of distinct owner/receiver index entries that can be created for a single victim address. Every successful delegation calls `delegateResourceAccountIndexStore.delegateV2(...)`, which unconditionally `put()`s a new key `V2_FROM_PREFIX + from + to` and `V2_TO_PREFIX + to + from` for every unique `(from, to)` pair: [3](#0-2) 

Because a low-cost attacker can freeze the minimal 1 TRX from many different self-controlled addresses (or, in the legacy V1 path, `delegate()` similarly grows entries under `FROM_PREFIX`/`TO_PREFIX`), the prefix range keyed by the victim's address can be grown arbitrarily large and cheaply, entry by entry, exactly as an attacker could mass-place bids on the auctioned NFT item in the original report.

The unbounded set is later reconstructed in full whenever `getIndex()`/`getV2Index()` is invoked, via `getWithPrefix()`, which performs a full `prefixQuery` over every entry for that address and sorts the entire result set in memory: [4](#0-3) 

This `getIndex`/`getV2Index` path backs the public `GetDelegatedResourceAccountIndex`/`GetDelegatedResourceAccountIndexV2` gRPC and HTTP endpoints (`RpcApiService`, `GetDelegatedResourceAccountIndexServlet`, `GetDelegatedResourceAccountIndexV2Servlet`), all reachable by anonymous API clients without authentication. The `convert()` migration path also walks the full to/from lists per address, so a bloated legacy index further amplifies cost during V1→V2 conversion: [5](#0-4) 

### Impact Explanation
An attacker can pre-poison a victim account (or even a well-known address, e.g., an exchange hot wallet or popular DApp) by delegating tiny resource amounts to it from a large number of freshly created accounts. Once the per-address prefix range holds a very large number of entries, any subsequent public query for that victim's delegated resource index forces the serving full node to iterate and sort the entire unbounded set in a single request. Because the query endpoints are unauthenticated and reachable by any API client, this can be used to degrade or exhaust the CPU/memory of full nodes serving that RPC/HTTP query, denying legitimate wallet/explorer/API service for that endpoint — a node-level resource-exhaustion DoS, matching the report's out-of-gas/DoS bug class translated to java-tron's storage/query layer instead of Solidity gas metering.

### Likelihood Explanation
The attack is cheap and fully permissionless: it only requires broadcasting many `DelegateResourceContract` transactions from disposable accounts, each needing just 1 TRX of frozen bandwidth/energy, and does not require the victim's cooperation or any special privilege. The query endpoints that trigger the expensive full-prefix scan are standard, always-on public Wallet APIs with no rate limiting or entry-count caps described in the reviewed code.

### Recommendation
- Cap the maximum number of distinct delegator/receiver index entries tracked per account (analogous to bounding "number of bids per item" in the report), rejecting further `DelegateResourceContract` calls once a victim's index size exceeds a configurable ceiling.
- Alternatively/additionally, paginate `getIndex()`/`getV2Index()` reads (similar to `getPaginatedNowWitnessList`) instead of always materializing and sorting the full prefix range in one call, and add limits/timeouts to prefix queries used by public API handlers.

### Proof of Concept
1. Create N attacker-controlled accounts, each freezing the minimum 1 TRX for bandwidth or energy (`FreezeBalanceV2Contract`).
2. From each of the N accounts, broadcast a `DelegateResourceContract` with `receiver_address` set to the victim address and `balance` = 1 TRX worth of frozen resource.
3. Each transaction is accepted by `DelegateResourceActuator.validate/execute` (no cap on distinct delegators/entries) and adds a new `V2_TO_PREFIX + victim + from` key via `DelegatedResourceAccountIndexStore.delegateV2`.
4. Repeat for large N (e.g., tens/hundreds of thousands) — cost scales only with N × 1 TRX (which can be recovered via `UnDelegateResourceContract`/unfreeze afterward), not with any list-size-dependent fee.
5. Call the public `GetDelegatedResourceAccountIndexV2` gRPC/HTTP endpoint for the victim address; the serving node executes `getV2Index` → `getWithPrefix`, performing a full prefix scan and sort over all N entries, consuming CPU/memory proportional to attacker-controlled N for every subsequent query of that address.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/DelegateResourceActuator.java (L147-150)
```java
    long delegateBalance = delegateResourceContract.getBalance();
    if (delegateBalance < TRX_PRECISION) {
      throw new ContractValidateException("delegateBalance must be greater than or equal to 1 TRX");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/DelegateResourceActuator.java (L192-209)
```java

    if (!DecodeUtil.addressValid(receiverAddress)) {
      throw new ContractValidateException("Invalid receiverAddress");
    }


    if (Arrays.equals(receiverAddress, ownerAddress)) {
      throw new ContractValidateException(
          "receiverAddress must not be the same as ownerAddress");
    }

    AccountCapsule receiverCapsule = accountStore.get(receiverAddress);
    if (receiverCapsule == null) {
      String readableOwnerAddress = StringUtil.createReadableString(receiverAddress);
      throw new ContractValidateException(
          ActuatorConstant.ACCOUNT_EXCEPTION_STR
              + readableOwnerAddress + NOT_EXIST_STR);
    }
```

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
