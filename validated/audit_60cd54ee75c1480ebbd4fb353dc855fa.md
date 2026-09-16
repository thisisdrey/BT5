Confirmed: `GetAssetIssueListServlet` and multiple `ListExchanges` HTTP/gRPC endpoints (full node, solidity node, PBFT node) all expose the fully-unpaginated `getExchangeList()` and asset-issue list, which iterate `getAllExchanges()` / `getAllAssetIssues()` over the entire store. [1](#0-0) [2](#0-1) [3](#0-2) 

### Title
Unbounded `ExchangeCreateContract` pool creation enables unpaginated `listExchanges`/`getExchangeList` API DoS - (File: `chainbase/src/main/java/org/tron/core/store/ExchangeStore.java`)

### Summary
`ExchangeCreateActuator` lets any account with the fixed `EXCHANGE_CREATE_FEE` create a new bancor-style exchange pair with no cap on the total number of exchanges that can exist on-chain. Every created exchange is persisted permanently in `ExchangeStore`/`ExchangeV2Store` and is included by `getAllExchanges()`, which is an unbounded, unpaginated full-table iteration exposed to any anonymous API caller via `listExchanges` (`RpcApiService`, `RpcApiServiceOnSolidity`, `RpcApiServiceOnPBFT`) and `ListExchangesServlet` (plus its Solidity/PBFT counterparts).

### Finding Description
`ExchangeCreateActuator.execute`/`doValidate` performs no check on the total number of exchanges in the system — the only constraints are the sender's TRX/token balance and the fixed `EXCHANGE_CREATE_FEE` (default 1024 TRX), unlike proposal creation, which is restricted to registered witnesses. [4](#0-3) 

Each successful transaction adds one more entry to `ExchangeStore`/`ExchangeV2Store`, and `dynamicStore.saveLatestExchangeNum(id)` simply increments a monotonically growing counter with no upper bound. [5](#0-4) 

`ExchangeStore.getAllExchanges()` streams and sorts the *entire* underlying RocksDB column family into an in-memory `List<ExchangeCapsule>` with no limit: [1](#0-0) 

`Wallet.getExchangeList()` calls this unbounded method directly to build a full `ExchangeList` protobuf response: [2](#0-1) 

This is reachable from unauthenticated JSON-RPC/HTTP clients via `ListExchangesServlet.doGet/doPost` (and identical solidity/PBFT variants), and via gRPC `listExchanges` in `RpcApiService`, `RpcApiServiceOnSolidity`, and `RpcApiServiceOnPBFT`. There is a paginated alternative (`getPaginatedExchangeList`, capped by `EXCHANGE_COUNT_LIMIT_MAX`) but it does not replace or gate the unpaginated endpoint, which remains publicly callable with no query parameters. [3](#0-2) [6](#0-5) 

This mirrors the reported Surge `deploySurgePool` DoS class: an unauthenticated actor can drive unbounded on-chain object creation (only gated by a flat, non-scaling fee) that is later fully materialized by a read path with no size limit, so the object count and the O(n) cost of every full-list query grow together without bound.

### Impact Explanation
Any account can pay the fixed exchange-creation fee repeatedly (looping many transactions, which is cheap relative to the resulting server-side amplification and requires no special privilege such as being a witness/SR) to grow `ExchangeStore` to an arbitrarily large size. Once the store is large, every call to the public, unauthenticated `listExchanges`/`GetExchangeList` API on any full node, solidity node, or PBFT node performs a full store scan, in-memory sort, and full protobuf/JSON serialization of the entire list on every single request. Because this endpoint takes no parameters and cannot be scoped down, repeated invocation by any anonymous client can drive sustained high CPU/memory usage on nodes serving these APIs, degrading or denying query service to legitimate users (an API the node can no longer reliably serve).

### Likelihood Explanation
Likelihood is high on the write side (creating exchanges only costs money, not privilege, and there's no maximum count check) and the read side is a default-enabled, unauthenticated HTTP/gRPC endpoint present on every full/solidity/PBFT node build. Growing the store to a size that makes the endpoint meaningfully expensive is a purely economic cost with no additional protocol restriction, and once grown, it affects every future caller of the unpaginated endpoint, not just the attacker.

### Recommendation
- Add an upper bound / rate-scaling fee for `ExchangeCreateContract` similar to protections used elsewhere (e.g., escalating fee or a maximum-outstanding-exchanges check) so the object count cannot grow unboundedly for a flat cost.
- Deprecate or restrict the unpaginated `listExchanges`/`GetExchangeList` endpoints (`ListExchangesServlet` and its Solidity/PBFT variants, and the corresponding gRPC methods) in favor of the already-existing paginated equivalent (`getPaginatedExchangeList`, bounded by `EXCHANGE_COUNT_LIMIT_MAX`), or add an internal hard cap/rate limit when serving the unpaginated call.
- Apply the same audit to `getAllAssetIssues()`/`GetAssetIssueListServlet`, `getAllProposals()`/proposal list endpoints, and any other `Streams.stream(iterator())`-based "getAll*" store methods reachable from public APIs.

### Proof of Concept
1. From an unprivileged account with sufficient TRX, broadcast many `ExchangeCreateContract` transactions (each requiring only the account to hold `EXCHANGE_CREATE_FEE` plus token balances for the pair), incrementing `LatestExchangeNum` and adding one row to `ExchangeStore`/`ExchangeV2Store` per transaction — no code path limits the total number of exchanges.
2. Repeat until the store holds a large number of entries (e.g., tens/hundreds of thousands), which is only bounded by the attacker's available funds and transaction throughput, not by protocol logic.
3. Send repeated unauthenticated HTTP `POST /wallet/listexchanges` or gRPC `listExchanges` requests (`ListExchangesServlet.doPost` → `wallet.getExchangeList()` → `ExchangeStore.getAllExchanges()`).
4. Observe that each request triggers a full RocksDB iteration, in-memory sort of every entry, and full protobuf/JSON serialization, causing elevated CPU/memory usage and increased response latency for the node under repeated requests, degrading the API's availability for all other callers.

### Citations

**File:** chainbase/src/main/java/org/tron/core/store/ExchangeStore.java (L28-38)
```java
  /**
   * get all exchanges.
   */
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

**File:** framework/src/main/java/org/tron/core/Wallet.java (L3307-3320)
```java
  public ExchangeList getPaginatedExchangeList(long offset, long limit) {
    if (limit < 0 || offset < 0) {
      return null;
    }

    long latestExchangeNum = chainBaseManager.getDynamicPropertiesStore()
        .getLatestExchangeNum();
    if (latestExchangeNum <= offset) {
      return null;
    }
    limit =
        limit > EXCHANGE_COUNT_LIMIT_MAX ? EXCHANGE_COUNT_LIMIT_MAX : limit;
    long end = offset + limit;
    end = end > latestExchangeNum ? latestExchangeNum : end;
```

**File:** framework/src/main/java/org/tron/core/services/http/ListExchangesServlet.java (L17-25)
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeCreateActuator.java (L104-119)
```java
      {
        // only save to new asset store
        ExchangeCapsule exchangeCapsuleV2 =
            new ExchangeCapsule(
                exchangeCreateContract.getOwnerAddress(),
                id,
                now,
                firstTokenID,
                secondTokenID
            );
        exchangeCapsuleV2.setBalance(firstTokenBalance, secondTokenBalance);
        exchangeV2Store.put(exchangeCapsuleV2.createDbKey(), exchangeCapsuleV2);
      }

      accountStore.put(accountCapsule.createDbKey(), accountCapsule);
      dynamicStore.saveLatestExchangeNum(id);
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeCreateActuator.java (L145-145)
```java
  private boolean doValidate() throws ContractValidateException {
```
