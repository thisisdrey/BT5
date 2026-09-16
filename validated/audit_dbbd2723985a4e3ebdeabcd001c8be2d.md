Based on my research, here is a valid analog to the reported MetaVesT unbounded-iteration issue.

### Title
Unbounded full-store iteration in `AssetIssueStore.getAllAssetIssues()` / `ExchangeStore.getAllExchanges()` enables node-level DoS via unprivileged asset/exchange creation - ([File: chainbase/src/main/java/org/tron/core/store/AssetIssueStore.java])

### Summary
Any account can create an `AssetIssueContract` (asset issuer persona) or `ExchangeCreateContract` (order/exchange placer persona) — bounded only by a small fixed fee, with no cap on the total number of assets or exchanges that can exist. Several API-facing methods in `Wallet` (reachable via HTTP/gRPC, e.g. `getAssetIssueList`, `getExchangeList`, `getAssetIssuesPaginated`) unconditionally load the *entire* `AssetIssueStore`/`ExchangeStore` contents into memory via `getAllAssetIssues()`/`getAllExchanges()`, and in the paginated variant additionally re-sort the whole in-memory list on every single request [1](#0-0) . This mirrors the reported MetaVesT bug class exactly: an ever-growing, attacker-extensible set that is fully iterated/sorted on every read, with cost scaling linearly (with sorting, worse than linearly) with the total number of entries ever created, with no `EnumerableSet`-like pruning or pagination cap on the underlying full scan.

### Finding Description
- `AssetIssueStore.getAllAssetIssues()` streams every key/value pair out of the RocksDB-backed `TronStoreWithRevoking` into an in-memory `List<AssetIssueCapsule>` with no bound [2](#0-1) .
- `getAssetIssuesPaginated` (used by both `GetAssetIssueListServlet`-style paginated APIs and `AssetIssueStore.getAssetIssuesPaginated(long, long)`) calls `getAllAssetIssues()` first, then performs an O(n log n) sort over the *entire* asset list before returning only the requested page [3](#0-2) . This means even a request for a single-item page forces a full-store fetch and full-store sort.
- The equivalent pattern exists for exchanges: `ExchangeStore.getAllExchanges()` streams and sorts the entire exchange list [4](#0-3) , and `Wallet.getExchangeList()` / `Wallet.getPaginatedExchangeList` invoke it directly on every call [5](#0-4) .
- Both `AssetIssueContract` creation (`AssetIssueActuator`) and `ExchangeCreateContract` creation (`ExchangeCreateActuator`) are open to any funded account with no privilege check beyond balance sufficiency and basic validation [6](#0-5) . An attacker can therefore continuously grow the `AssetIssueStore`/`ExchangeStore` by repeatedly issuing minimal assets or creating exchanges, since `AllowSameTokenName` mode allows unlimited numbers of tokens per account and there is no upper bound on total token/exchange count enforced anywhere in these actuators.
- Every subsequent call to the list/paginated list APIs (exposed over HTTP/gRPC to any anonymous client, e.g. `ListExchangesServlet`, `GetPaginatedProposalListServlet`-equivalents for assets/exchanges) becomes progressively more expensive as the attacker-controlled store grows, exactly analogous to the reported `consentCheck` iterating over all MetaVesT sets.

### Impact Explanation
As the number of assets/exchanges grows without bound (attacker-controlled, paid only in cheap creation fees), the cost (CPU time and memory allocation) of servicing `getAssetIssueList`, `getExchangeList`, and their paginated variants grows linearly to super-linearly. Because full nodes expose these RPC/HTTP endpoints to anonymous API clients, a sufficiently large store (built cheaply over time by an attacker) can cause sustained high CPU/memory usage on every list query, degrading node responsiveness for all API consumers and potentially exhausting node memory (full materialization of the entire store plus a sort buffer) — a node-level denial of service, matching the "node crash or halt" / "API the node can no longer serve" impact criteria from the report's bug class.

### Likelihood Explanation
High: creating assets and exchanges requires only ordinary account privileges and a fixed, moderate fee (`getAssetIssueFee` / `getExchangeCreateFee`), each independently controlled by chain parameters and not scaled to store size. No proposal, witness, or committee privilege is required, and no existing validate() logic caps total supply/count of assets or exchanges system-wide. An attacker with modest capital can grow these stores over time to sizes that make repeated `getAllAssetIssues()`/`getAllExchanges()` calls prohibitively expensive for full nodes serving public API traffic.

### Recommendation
- Avoid full-store materialization + full in-memory sort for paginated queries; instead maintain an ordered index (e.g., an incrementing ID/sequence key, similar to `ProposalStore`/`ExchangeStore`'s `calculateDbKey(long number)` range-scan pattern already used in `getPaginatedProposalList`/`getPaginatedExchangeList`) and read only the requested key range directly from the underlying store without loading the entire dataset.
- For unpaginated `getAllAssetIssues()`/`getAllExchanges()`/`getAssetIssueList()` endpoints, enforce a hard result cap (matching the existing `ASSET_ISSUE_COUNT_LIMIT_MAX`/`WITNESS_COUNT_LIMIT_MAX`/`EXCHANGE_COUNT_LIMIT_MAX` patterns already used elsewhere) or require pagination for all callers.
- Consider adding a global cap or fee-scaling mechanism on asset/exchange creation so store growth costs increase with total store size, discouraging cheap unbounded growth.

### Proof of Concept
1. From any funded account, repeatedly broadcast `AssetIssueContract` transactions (with `AllowSameTokenName=1`) or `ExchangeCreateContract` transactions, each costing only the fixed `getAssetIssueFee`/`getExchangeCreateFee`, to grow `AssetIssueStore`/`ExchangeStore` to a very large number of entries (e.g., hundreds of thousands).
2. As an anonymous API client, repeatedly call `wallet/getassetissuelist`, `wallet/getpaginatedassetissuelist`, or `wallet/getpaginatedexchangelist` (or the gRPC equivalents in `RpcApiService`) with a small `limit`.
3. Observe that response latency and node memory/CPU usage for these calls scale with the total number of assets/exchanges ever created (due to `getAllAssetIssues()`/`getAllExchanges()` fully materializing and sorting the store on every call), even though only a small page is requested, eventually degrading or denying service on the queried full node as the attacker continues step 1. [1](#0-0) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** chainbase/src/main/java/org/tron/core/store/AssetIssueStore.java (L34-63)
```java
  public List<AssetIssueCapsule> getAllAssetIssues() {
    return Streams.stream(iterator())
        .map(Entry::getValue)
        .collect(Collectors.toList());
  }

  private List<AssetIssueCapsule> getAssetIssuesPaginated(List<AssetIssueCapsule> assetIssueList,
      long offset, long limit) {
    if (limit < 0 || offset < 0) {
      return null;
    }

    if (assetIssueList.size() <= offset) {
      return null;
    }
    assetIssueList.sort((o1, o2) -> {
      if (o1.getName() != o2.getName()) {
        return o1.getName().toStringUtf8().compareTo(o2.getName().toStringUtf8());
      }
      return Long.compare(o1.getOrder(), o2.getOrder());
    });
    limit = limit > ASSET_ISSUE_COUNT_LIMIT_MAX ? ASSET_ISSUE_COUNT_LIMIT_MAX : limit;
    long end = offset + limit;
    end = end > assetIssueList.size() ? assetIssueList.size() : end;
    return assetIssueList.subList((int) offset, (int) end);
  }

  public List<AssetIssueCapsule> getAssetIssuesPaginated(long offset, long limit) {
    return getAssetIssuesPaginated(getAllAssetIssues(), offset, limit);
  }
```

**File:** chainbase/src/main/java/org/tron/core/store/ExchangeStore.java (L31-38)
```java
  public List<ExchangeCapsule> getAllExchanges() {
    return Streams.stream(iterator())
        .map(Map.Entry::getValue)
        .sorted(
            (ExchangeCapsule a, ExchangeCapsule b) -> a.getCreateTime() <= b.getCreateTime() ? 1
                : -1)
        .collect(Collectors.toList());
  }
```

**File:** framework/src/main/java/org/tron/core/Wallet.java (L1066-1076)
```java
  public ExchangeList getExchangeList() {
    ExchangeList.Builder builder = ExchangeList.newBuilder();
    List<ExchangeCapsule> exchangeCapsuleList =
        getExchangeStoreFinal(chainBaseManager.getDynamicPropertiesStore(),
            chainBaseManager.getExchangeStore(),
            chainBaseManager.getExchangeV2Store()).getAllExchanges();

    exchangeCapsuleList
        .forEach(exchangeCapsule -> builder.addExchanges(exchangeCapsule.getInstance()));
    return builder.build();
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeCreateActuator.java (L145-182)
```java
  private boolean doValidate() throws ContractValidateException {
    if (this.any == null) {
      throw new ContractValidateException(ActuatorConstant.CONTRACT_NOT_EXIST);
    }
    if (chainBaseManager == null) {
      throw new ContractValidateException(ActuatorConstant.STORE_NOT_EXIST);
    }
    AccountStore accountStore = chainBaseManager.getAccountStore();
    DynamicPropertiesStore dynamicStore = chainBaseManager.getDynamicPropertiesStore();
    if (!this.any.is(ExchangeCreateContract.class)) {
      throw new ContractValidateException(
          "contract type error,expected type [ExchangeCreateContract],real type[" + any
              .getClass() + "]");
    }
    final ExchangeCreateContract contract;
    try {
      contract = this.any.unpack(ExchangeCreateContract.class);
    } catch (InvalidProtocolBufferException e) {
      throw new ContractValidateException(e.getMessage());
    }

    byte[] ownerAddress = contract.getOwnerAddress().toByteArray();
    String readableOwnerAddress = StringUtil.createReadableString(ownerAddress);

    if (!DecodeUtil.addressValid(ownerAddress)) {
      throw new ContractValidateException("Invalid address");
    }

    if (!accountStore.has(ownerAddress)) {
      throw new ContractValidateException("account[" + readableOwnerAddress + NOT_EXIST_STR);
    }

    AccountCapsule accountCapsule = accountStore.get(ownerAddress);

    if (accountCapsule.getBalance() < calcFee()) {
      throw new ContractValidateException("No enough balance for exchange create fee!");
    }

```
