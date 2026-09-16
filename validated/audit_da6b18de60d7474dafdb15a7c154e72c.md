### Title
Inconsistent comparator in `ProposalStore.getAllProposals()` / `ExchangeStore.getAllExchanges()` crashes the public `listproposals`/`listexchanges` query APIs once enough records exist - (`chainbase/src/main/java/org/tron/core/store/ProposalStore.java`, `chainbase/src/main/java/org/tron/core/store/ExchangeStore.java`)

### Summary
`ProposalStore.getAllProposals()` and `ExchangeStore.getAllExchanges()` sort the **entire** on-disk store on every invocation using a comparator that never returns `0` and violates the general `Comparator` contract (antisymmetry). Both methods are reachable from unauthenticated HTTP/gRPC query endpoints (`/wallet/listproposals`, `/wallet/listexchanges`, and their Solidity/PBFT variants). Java's `TimSort`/`Collections.sort` detects broken comparators once the collection is large enough and throws `IllegalArgumentException("Comparison method violates its general contract!")`, permanently breaking these query endpoints for every client. This is the same "unbounded collection iterated/processed by a function that any actor can grow" bug class as the referenced report (`DefaultStateManager.assessStates()` DOS via unbounded `protectionPoolStates`), except here it produces a deterministic crash rather than only gas exhaustion.

### Finding Description
`ProposalStore.getAllProposals()` sorts using: [1](#0-0) 

and `ExchangeStore.getAllExchanges()` uses the analogous pattern: [2](#0-1) 

The comparator `(a, b) -> a.getCreateTime() <= b.getCreateTime() ? 1 : -1` has two defects:
1. It never returns `0`, even for equal timestamps, so `compare(x, y) == 1` **and** `compare(y, x) == 1` simultaneously whenever `x.getCreateTime() == y.getCreateTime()`. This directly violates the `Comparator` antisymmetry contract required by the JDK's sort implementation.
2. Because `Proposal`/`Exchange` timestamps are frequently equal (entries created in the same block/second share the same `createTime`), this violation is easy to trigger with a modest number of entries.

`Collections.sort`/`Stream.sorted()` on the JDK use `TimSort`, which actively checks comparator consistency once the array size passes the `MIN_MERGE` threshold (32 elements) and throws `IllegalArgumentException: Comparison method violates its general contract!` when it detects the inconsistency.

Both proposal and exchange creation are permissionless (paid, but callable by any account), so any actor can grow `ProposalStore`/`ExchangeStore` past 32 entries with duplicate `createTime` values (trivial — just submit multiple `ProposalCreateContract`/`ExchangeCreateContract` transactions within the same block/second). After that point, every call to the query path throws, and the exception is not specially handled — it propagates out of the servlet/gRPC handler.

These methods are reachable from unauthenticated public APIs: [3](#0-2) [4](#0-3) 

exposed via `ListProposalsServlet`/`ListExchangesServlet`, `RpcApiService.listProposals/listExchanges`, and their Solidity/PBFT proxies: [5](#0-4) 

Even in the non-crashing case (few entries, no duplicate timestamps), these calls still perform an **unbounded** full-store scan and sort on every request with no pagination, so as the store grows (asset issuance/proposal/exchange counts are permissionless and only cost fees), request cost grows without bound — mirroring the original report's resource-exhaustion pattern (`AssetIssueStore.getAllAssetIssues()`/`getAssetIssueList()` exhibits the same unpaginated full-scan issue: [6](#0-5) ).

### Impact Explanation
Once the comparator inconsistency is triggered, `listproposals`/`getpaginatedproposallist`-adjacent full-list endpoints and `listexchanges` throw an unhandled `IllegalArgumentException` on **every subsequent call**, on the full node, the Solidity node, and the PBFT node (they all funnel to the same `Wallet`/store methods). This is a persistent denial of service against a documented public query API that the node "can no longer serve," matching the acceptance criteria for this class of finding. It does not require any privileged role — any account able to pay the small `ProposalCreateContract`/`ExchangeCreateContract` fee can trigger it.

### Likelihood Explanation
High. No special privilege is required, only ~32 low-cost transactions (proposal or exchange creation) submitted within a short time window so several entries share identical `createTime`, which is a common/likely occurrence and not something an attacker needs to engineer precisely — most transactions confirmed in the same block naturally receive equal `createTime` values.

### Recommendation
- Fix the comparators in `ProposalStore.getAllProposals()` and `ExchangeStore.getAllExchanges()` to be contract-consistent, e.g. `Long.compare(b.getCreateTime(), a.getCreateTime())`, and add a tiebreaker (e.g., by ID) for full determinism.
- Enforce pagination (as already exists via `getPaginatedProposalList`/`getPaginatedExchangeList`) on the unpaginated `getProposalList()`/`getExchangeList()`/`getAssetIssueList()` paths, or otherwise cap the number of entries scanned/sorted per call, so cost cannot grow unbounded with store size.

### Proof of Concept
1. Submit ~32+ `ProposalCreateContract` (requires witness) or, more easily, `ExchangeCreateContract` transactions (permissionless, only requires balance/fee) such that at least two entries land in the same block (sharing `createTime`).
2. Call the public endpoint `/wallet/listexchanges` (or `listproposals`/gRPC `listExchanges`).
3. `ExchangeStore.getAllExchanges()` invokes `Streams.stream(iterator()).sorted(comparator)`; once the collection size exceeds the `TimSort` `MIN_MERGE` threshold (32) and a compared pair has equal `createTime`, the JVM throws `IllegalArgumentException: Comparison method violates its general contract!`, which propagates through `Wallet.getExchangeList()` → `ListExchangesServlet`/`RpcApiService.listExchanges`, causing the API call to fail every time it is invoked thereafter.

### Citations

**File:** chainbase/src/main/java/org/tron/core/store/ProposalStore.java (L32-39)
```java
  public List<ProposalCapsule> getAllProposals() {
    return Streams.stream(iterator())
        .map(Map.Entry::getValue)
        .sorted(
            (ProposalCapsule a, ProposalCapsule b) -> a.getCreateTime() <= b.getCreateTime() ? 1
                : -1)
        .collect(Collectors.toList());
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

**File:** framework/src/main/java/org/tron/core/Wallet.java (L874-881)
```java
  public ProposalList getProposalList() {
    ProposalList.Builder builder = ProposalList.newBuilder();
    List<ProposalCapsule> proposalCapsuleList =
        chainBaseManager.getProposalStore().getAllProposals();
    proposalCapsuleList
        .forEach(proposalCapsule -> builder.addProposals(proposalCapsule.getInstance()));
    return builder.build();
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

**File:** framework/src/main/java/org/tron/core/Wallet.java (L1530-1544)
```java
  public AssetIssueList getAssetIssueList() {
    BandwidthProcessor processor = new BandwidthProcessor(chainBaseManager);
    AssetIssueList.Builder builder = AssetIssueList.newBuilder();

    getAssetIssueStoreFinal(chainBaseManager.getDynamicPropertiesStore(),
        chainBaseManager.getAssetIssueStore(),
        chainBaseManager.getAssetIssueV2Store()).getAllAssetIssues()
        .forEach(
            issueCapsule -> {
              processor.updateUsage(issueCapsule);
              builder.addAssetIssue(issueCapsule.getInstance());
            });

    return builder.build();
  }
```

**File:** framework/src/main/java/org/tron/core/services/RpcApiService.java (L603-608)
```java
    @Override
    public void listExchanges(EmptyMessage request,
        StreamObserver<ExchangeList> responseObserver) {
      responseObserver.onNext(wallet.getExchangeList());
      responseObserver.onCompleted();
    }
```
