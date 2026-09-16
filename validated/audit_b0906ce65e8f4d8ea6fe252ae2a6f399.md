### Title
Unbounded, unpaginated REST/gRPC list endpoints (`/wallet/getassetissuelist`, `/wallet/listexchanges`, `/wallet/listwitnesses`, `/wallet/listnodes`) allow unauthenticated resource-exhaustion / OOM DoS - (File: `framework/src/main/java/org/tron/core/services/http/GetAssetIssueListServlet.java`)

### Summary
Several java-tron HTTP/gRPC endpoints that enumerate on-chain collections have no size limit and no pagination requirement, unlike their "paginated" counterparts. An unauthenticated caller can repeatedly hit these endpoints to force the node to materialize and serialize the entire in-memory/DB collection (all asset issues, all exchanges, all witnesses, or all connected peer nodes) into a JSON/protobuf response, which is the same bug class described in the XWiki advisory (REST APIs without limits returning "all pages"/"all items" leading to slowness, unavailability, and OOM).

### Finding Description
`GetAssetIssueListServlet.doGet` calls `wallet.getAssetIssueList()` with no offset/limit and writes the full serialized result directly to the response [1](#0-0) . This differs from the "paginated" sibling endpoint `GetPaginatedAssetIssueListServlet`, which clamps `limit` to `ASSET_ISSUE_COUNT_LIMIT_MAX` via `AssetIssueStore.getAssetIssuesPaginated` [2](#0-1)  and `Wallet.getPaginatedProposalList` [3](#0-2) . However, the unbounded variants remain reachable and unauthenticated:

- `/wallet/getassetissuelist` → `GetAssetIssueListServlet` → `wallet.getAssetIssueList()` (all asset issues, no cap) [1](#0-0) 
- `/wallet/listexchanges` → `ListExchangesServlet` → `wallet.getExchangeList()` (all exchange pairs, no cap) [4](#0-3) 
- `/wallet/listwitnesses` and `listWitnesses` gRPC → `wallet.getWitnessList()` (all witnesses, unbounded, but witness count is protocol-limited so lower severity)
- `/wallet/listnodes` → `ListNodesServlet` → `wallet.listNodes()` (full peer/node list, no cap) [5](#0-4) 

Both the FullNode HTTP API and the SolidityNode/PBFT HTTP API register these same unbounded servlets (`getAssetIssueListServlet`, `listExchangesServlet`, `listNodesServlet`) [6](#0-5) [7](#0-6) , so both node types are affected, and the equivalent gRPC calls (`WalletSolidityApi.getAssetIssueList`, etc.) also return the unbounded collection without any server-side truncation [8](#0-7) .

The only mitigation present is `RateLimiterServlet` (rate limiting requests per time window) and `Util.checkBodySize` (limits request body size), neither of which limits the size of the *response* being constructed and serialized on each call. As the number of asset issues, exchange pairs, or peer connections on a wiki-scale/production TRON network grows, each single unauthenticated call forces full-collection iteration, sorting/serialization, and allocation proportional to the total dataset size — this is directly analogous to XWiki's `/rest/wikis/xwiki/spaces` returning "all pages" by default.

### Impact Explanation
An unauthenticated client can send a burst of requests to `/wallet/getassetissuelist`, `/wallet/listexchanges`, or `/wallet/listnodes` (or their `walletsolidity`/PBFT/gRPC equivalents), each of which forces the node to build and serialize the entire in-memory collection. On a wiki with many assets/exchanges (java-tron mainnet has tens of thousands of TRC10 assets), this can consume significant CPU and heap per request, and concurrent repeated requests can exhaust node memory or degrade block processing/validation, resulting in denial of service. This matches the CWE-770 "Allocation of Resources Without Limits" classification in the referenced advisory, and can degrade or crash a public FullNode/SolidityNode, impacting its ability to serve the network (CVSS VA:H analog).

### Likelihood Explanation
These endpoints require no authentication, no special permissions, and no on-chain transaction — a plain HTTP GET/POST or gRPC unary call. They are enabled by default whenever `fullNodeHttpEnable`/`solidityNodeHttpEnable` is on (the standard operating mode). The only barrier is `RateLimiterServlet`, which throttles request rate but does not limit response payload size per request, so a low-and-slow or distributed attacker can still repeatedly trigger expensive full-collection serialization. Likelihood is high given the low complexity and default exposure.

### Recommendation
Apply the same pagination/limit enforcement used in `GetPaginatedAssetIssueListServlet`/`AssetIssueStore.getAssetIssuesPaginated` to the unbounded variants, or deprecate/cap them (e.g., default and maximum response-size limits similar to XWiki's fix of capping to 1000 items and rejecting requests for larger limits) for `GetAssetIssueListServlet`, `ListExchangesServlet`, `ListNodesServlet`, and their PBFT/Solidity/gRPC equivalents.

### Proof of Concept
1. Deploy or point at any FullNode with HTTP API enabled (default config).
2. Send repeated concurrent unauthenticated requests: `curl http://<node>:8090/wallet/getassetissuelist` and `curl -X POST http://<node>:8090/wallet/listexchanges` and `curl http://<node>:8090/wallet/listnodes`.
3. Observe that each request causes full iteration/serialization of the entire asset-issue store / exchange store / peer list, with response size and latency scaling with node's total state size; repeated concurrent calls increase heap usage and can starve other request-handling threads, degrading node availability — with no server-side limit preventing this.

### Citations

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

**File:** chainbase/src/main/java/org/tron/core/store/AssetIssueStore.java (L40-59)
```java
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
```

**File:** framework/src/main/java/org/tron/core/Wallet.java (L3275-3305)
```java
  public ProposalList getPaginatedProposalList(long offset, long limit) {

    if (limit < 0 || offset < 0) {
      return null;
    }

    long latestProposalNum = chainBaseManager.getDynamicPropertiesStore()
        .getLatestProposalNum();
    if (latestProposalNum <= offset) {
      return null;
    }
    limit =
        limit > PROPOSAL_COUNT_LIMIT_MAX ? PROPOSAL_COUNT_LIMIT_MAX : limit;
    long end = offset + limit;
    end = end > latestProposalNum ? latestProposalNum : end;
    ProposalList.Builder builder = ProposalList.newBuilder();

    ImmutableList<Long> rangeList = ContiguousSet
        .create(Range.openClosed(offset, end), DiscreteDomain.longs())
        .asList();
    rangeList.stream().map(ProposalCapsule::calculateDbKey).map(key -> {
      try {
        return chainBaseManager.getProposalStore().get(key);
      } catch (Exception ex) {
        return null;
      }
    }).filter(Objects::nonNull)
        .forEach(proposalCapsule -> builder
            .addProposals(proposalCapsule.getInstance()));
    return builder.build();
  }
```

**File:** framework/src/main/java/org/tron/core/services/http/ListExchangesServlet.java (L18-25)
```java
  protected void doPost(HttpServletRequest request, HttpServletResponse response) {
    try {
      boolean visible = Util.getVisible(request);
      response.getWriter().println(JsonFormat.printToString(wallet.getExchangeList(), visible));
    } catch (Exception e) {
      Util.processError(e, response);
    }
  }
```

**File:** framework/src/main/java/org/tron/core/services/http/ListNodesServlet.java (L18-31)
```java
  @Override
  protected void doGet(HttpServletRequest request, HttpServletResponse response) {
    try {
      boolean visible = Util.getVisible(request);
      NodeList reply = wallet.listNodes();
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

**File:** framework/src/main/java/org/tron/core/services/http/FullNodeHttpApiService.java (L349-360)
```java
    context.addServlet(new ServletHolder(listWitnessesServlet), "/wallet/listwitnesses");
    // Get the paged list of witnesses info with realtime vote counts
    context.addServlet(new ServletHolder(getPaginatedNowWitnessListServlet),
        "/wallet/getpaginatednowwitnesslist");
    context.addServlet(new ServletHolder(getAssetIssueListServlet), "/wallet/getassetissuelist");
    context.addServlet(
        new ServletHolder(getPaginatedAssetIssueListServlet),
        "/wallet/getpaginatedassetissuelist");
    context.addServlet(
        new ServletHolder(getPaginatedProposalListServlet), "/wallet/getpaginatedproposallist");
    context.addServlet(
        new ServletHolder(getPaginatedExchangeListServlet), "/wallet/getpaginatedexchangelist");
```

**File:** framework/src/main/java/org/tron/core/services/http/solidity/SolidityNodeHttpApiService.java (L90-100)
```java
  private GetExchangeByIdServlet getExchangeByIdServlet;
  @Autowired
  private ListExchangesServlet listExchangesServlet;
  @Autowired
  private ListWitnessesServlet listWitnessesServlet;
  @Autowired
  private GetPaginatedNowWitnessListServlet getPaginatedNowWitnessListServlet;
  @Autowired
  private GetAssetIssueListServlet getAssetIssueListServlet;
  @Autowired
  private GetPaginatedAssetIssueListServlet getPaginatedAssetIssueListServlet;
```

**File:** framework/src/main/java/org/tron/core/services/RpcApiService.java (L411-416)
```java
    @Override
    public void getAssetIssueList(EmptyMessage request,
        StreamObserver<AssetIssueList> responseObserver) {
      responseObserver.onNext(wallet.getAssetIssueList());
      responseObserver.onCompleted();
    }
```
