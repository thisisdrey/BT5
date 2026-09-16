### Title
Unbounded, non-paginated `getAssetIssueList` full-table scan enables cheap, permissionless API DoS - ([File: framework/src/main/java/org/tron/core/Wallet.java])

### Summary
`AssetIssueActuator` lets any account permissionlessly create new tokens with no cap on the total number of assets ever issued (only the per-transaction `FrozenSupply` list length is capped by `MaxFrozenSupplyNumber`). Every issued asset is stored permanently in `AssetIssueStore`/`AssetIssueV2Store`. `Wallet.getAssetIssueList()` performs a full, unbounded scan of that store on every call — no pagination, no limit — and is exposed both over HTTP (`GetAssetIssueListServlet`) and gRPC (`RpcApiService`), reachable by any anonymous API client. This is the direct analog of the reported `tokenList` DOS pattern: an attacker-growable array that is fully iterated by a hot, publicly reachable path.

### Finding Description
`AssetIssueActuator.execute()` allows any address to create a new asset issue for a fee, incrementing `tokenIdNum` with no upper bound on the total number of assets in the system: [1](#0-0) 

Each new asset is permanently written into `AssetIssueStore`/`AssetIssueV2Store`, and `AssetIssueStore.getAllAssetIssues()` streams the *entire* store contents into an in-memory list: [2](#0-1) 

`Wallet.getAssetIssueList()` (the unpaginated variant, distinct from `getAssetIssueList(offset, limit)`) calls `getAllAssetIssues()` and then, for every single asset, invokes `BandwidthProcessor.updateUsage(issueCapsule)` — an extra per-item computation/DB touch — before serializing the whole list into a response: [3](#0-2) 

This method (and the very similar `getAssetIssueByAccount`/`getAssetIssueListByName`, which also call `getAllAssetIssues()` and then filter) is exposed to any unauthenticated caller via:
- HTTP: `GetAssetIssueListServlet.doGet()` → `wallet.getAssetIssueList()` [4](#0-3) 
- gRPC: `RpcApiService` (same method), plus mirrored Solidity/PBFT read-only node interfaces.

Because the underlying token count is attacker-controlled and unbounded (bounded only by economic cost of the issuance fee, not by any protocol-level cap), an attacker can inflate `AssetIssueStore`/`AssetIssueV2Store` to a very large size over time, after which every call to the unpaginated list/lookup-by-name/lookup-by-account endpoints becomes O(N) in both CPU (per-item bandwidth usage recompute) and memory/serialization (building and returning the full protobuf list), with no limit applied by the endpoint itself.

### Impact Explanation
Once the asset table is inflated, every unauthenticated caller of `getAssetIssueList()` / `getAssetIssueByAccount()` / `getAssetIssueListByName()` forces the node to perform a full scan, per-item bandwidth recomputation, and large-object serialization. Repeated invocation of these public, un-rate-limited-by-size endpoints can degrade or exhaust node CPU/memory, denying the API service to legitimate clients — matching the "API the node can no longer serve" acceptance criterion. This does not affect consensus-critical block application paths directly, so it is scoped as a node/API-availability DoS rather than a chain-halt, but it is reachable purely from ordinary, permissionless transactions (asset issuance) plus ordinary API requests — no privileged role required.

### Likelihood Explanation
High, in principle: any account can call `AssetIssueContract` repeatedly (paying the issuance fee each time, with no explicit total-count cap found in `AssetIssueActuator.validate()`), and any anonymous client can then hit the unpaginated `getAssetIssueList` HTTP/gRPC endpoint. The severity in practice depends on the economic cost of the per-token issuance fee versus the marginal cost imposed on node operators by repeated full scans — this is a resource-asymmetry question that would need to be validated against current `AssetIssueFee`/`assetIssueFee` dynamic parameters, which were not directly inspected here (only `calcFee()` and TRX-burn logic in the actuator were reviewed, not the current fee value).

### Recommendation
- Deprecate/remove or hard-cap the unpaginated `getAssetIssueList()`, `getAssetIssueByAccount()`, and `getAssetIssueListByName()` APIs, forcing all clients through the already-existing paginated path (`getAssetIssueList(offset, limit)` via `GetPaginatedAssetIssueListServlet`), which already applies `ASSET_ISSUE_COUNT_LIMIT_MAX`.
- Alternatively, apply the same `ASSET_ISSUE_COUNT_LIMIT_MAX` cap and avoid full-list materialization in the unpaginated variants, or maintain name/owner secondary indexes so `getAssetIssueByAccount`/`getAssetIssueListByName` don't need to scan and filter every asset.
- Consider introducing an upper bound (or an increasing issuance-fee curve) on the total number of assets a single account (or the network) can issue, to bound worst-case scan cost independent of API-layer mitigations.

### Proof of Concept
1. From an unprivileged account, submit `AssetIssueContract` transactions repeatedly (paying the per-issuance fee each time) to grow `AssetIssueStore`/`AssetIssueV2Store` to a large number of entries — nothing in `AssetIssueActuator.validate()`/`execute()` caps the total count of assets in the store. [5](#0-4) 
2. As an anonymous API client, repeatedly call the HTTP endpoint `GET /wallet/getassetissuelist` (backed by `GetAssetIssueListServlet` → `Wallet.getAssetIssueList()`), or the equivalent gRPC `GetAssetIssueList` call. [4](#0-3) 
3. Each call forces `AssetIssueStore.getAllAssetIssues()` to stream and materialize every stored asset, followed by an O(N) bandwidth-usage update loop and full protobuf serialization, with no size cap applied by this code path (unlike its paginated sibling). [3](#0-2) [6](#0-5) 
4. Repeated concurrent calls to this endpoint by the attacker impose growing CPU/memory cost on the serving node, degrading its ability to serve legitimate API traffic.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/AssetIssueActuator.java (L55-87)
```java
  @Override
  public boolean execute(Object result) throws ContractExeException {
    TransactionResultCapsule ret = (TransactionResultCapsule) result;
    if (Objects.isNull(ret)) {
      throw new RuntimeException(ActuatorConstant.TX_RESULT_NULL);
    }

    long fee = calcFee();
    DynamicPropertiesStore dynamicStore = chainBaseManager.getDynamicPropertiesStore();
    AssetIssueStore assetIssueStore = chainBaseManager.getAssetIssueStore();
    AssetIssueV2Store assetIssueV2Store = chainBaseManager.getAssetIssueV2Store();
    AccountStore accountStore = chainBaseManager.getAccountStore();
    try {
      AssetIssueContract assetIssueContract = any.unpack(AssetIssueContract.class);
      byte[] ownerAddress = assetIssueContract.getOwnerAddress().toByteArray();
      AssetIssueCapsule assetIssueCapsule = new AssetIssueCapsule(assetIssueContract);
      AssetIssueCapsule assetIssueCapsuleV2 = new AssetIssueCapsule(assetIssueContract);
      long tokenIdNum = dynamicStore.getTokenIdNum();
      tokenIdNum++;
      assetIssueCapsule.setId(Long.toString(tokenIdNum));
      assetIssueCapsuleV2.setId(Long.toString(tokenIdNum));
      dynamicStore.saveTokenIdNum(tokenIdNum);

      if (dynamicStore.getAllowSameTokenName() == 0) {
        assetIssueCapsuleV2.setPrecision(0);
        assetIssueStore
            .put(assetIssueCapsule.createDbKey(), assetIssueCapsule);
        assetIssueV2Store
            .put(assetIssueCapsuleV2.createDbV2Key(), assetIssueCapsuleV2);
      } else {
        assetIssueV2Store
            .put(assetIssueCapsuleV2.createDbV2Key(), assetIssueCapsuleV2);
      }
```

**File:** chainbase/src/main/java/org/tron/core/store/AssetIssueStore.java (L31-38)
```java
  /**
   * get all asset issues.
   */
  public List<AssetIssueCapsule> getAllAssetIssues() {
    return Streams.stream(iterator())
        .map(Entry::getValue)
        .collect(Collectors.toList());
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

**File:** framework/src/main/java/org/tron/core/services/http/GetAssetIssueListServlet.java (L18-31)
```java
  @Override
  protected void doGet(HttpServletRequest request, HttpServletResponse response) {
    try {
      boolean visible = Util.getVisible(request);
      AssetIssueList reply = wallet.getAssetIssueList();
      if (reply != null) {
        response.getWriter().println(JsonFormat.printToString(reply, visible));
      } else {
        response.getWriter().println("{}");
      }
    } catch (Exception e) {
      Util.processError(e, response);
    }
  }
```
