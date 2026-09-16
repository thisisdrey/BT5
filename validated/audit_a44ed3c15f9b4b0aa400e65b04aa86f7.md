### Title
Out-of-Gas / Unbounded Memory Growth in `ExchangeStore.getAllExchanges()` Enables DoS of `listExchanges` API via Unlimited TRC10 Exchange Creation - (File: `chainbase/src/main/java/org/tron/core/store/ExchangeStore.java`)

### Summary
Any unprivileged account can call `ExchangeCreateActuator` to register a new TRC10 exchange pair via a standard signed transaction, with no upper bound on the total number of exchanges (`totalExchangeNum` grows unbounded). The `listExchanges` API path (`Wallet.getExchangeList()` → `ExchangeStore.getAllExchanges()`) then iterates and materializes *all* exchanges into memory with **no limit**, unlike the paginated variant `getPaginatedExchangeList`, which explicitly caps results via `EXCHANGE_COUNT_LIMIT_MAX`. This directly mirrors the reported Perennial Vault bug class: unbounded registration by an unprivileged actor combined with an unbounded loop in a reachable function.

### Finding Description
`ExchangeCreateActuator.execute` lets any account with sufficient balance create a new exchange pair, incrementing `dynamicStore.getLatestExchangeNum()`/`saveLatestExchangeNum(id)` without any maximum cap on the number of exchanges that can exist: [1](#0-0) 

`ExchangeStore.getAllExchanges()` loads every stored `ExchangeCapsule` from the underlying revoking DB into a `List` in a single unbounded pass, with no size limit or pagination: [2](#0-1) 

This unbounded loader is invoked by `Wallet.getExchangeList()`, which iterates the full result set to build the protobuf response: [3](#0-2) 

`Wallet.getExchangeList()` is exposed through multiple unauthenticated, anonymous-reachable API surfaces: the gRPC `listExchanges` RPC in `RpcApiService`, and the HTTP `ListExchangesServlet` (plus equivalent PBFT/Solidity node servlets): [4](#0-3) [5](#0-4) 

Notably, the codebase already recognizes this pattern is dangerous and mitigates it *only* for the paginated variant, `Wallet.getPaginatedExchangeList`, which bounds results with `EXCHANGE_COUNT_LIMIT_MAX`: [6](#0-5) 

However, `getExchangeList`/`listExchanges` (unpaginated) bypasses this protection entirely, retaining the exact unbounded-loop hazard the report describes.

### Impact Explanation
Since `ExchangeCreateContract` is an ordinary user transaction (subject only to an account-balance/fee check, not any admin permission), an attacker with modest and repeatable capital can create a very large number of exchange pairs over time. Once the total exchange count is large enough, every future call to the unpaginated `listExchanges` gRPC method or the `/wallet/listexchanges` HTTP endpoint (and its Solidity/PBFT-node siblings) forces the node to enumerate the entire exchange store and construct a correspondingly large in-memory protobuf response. This can exhaust node CPU/memory or JVM response-time thresholds, denying the API to all other unprivileged clients — i.e., "an API the node can no longer serve," which is explicitly accepted impact for this analog.

Because full/witness nodes typically expose these HTTP/gRPC APIs publicly, and the underlying growth is driven entirely by ordinary, permissionless transactions rather than any privileged/malicious-SR action, this satisfies the scope requirement that the vulnerability be reachable from an unprivileged transaction/API client.

### Likelihood Explanation
The cost to grow the exchange list is bounded only by the exchange-create fee and available token balances of the attacker (fee mechanics are visible in `ExchangeCreateActuator.doValidate`, requiring the account balance to cover `calcFee()` plus deposited token amounts) — there is no cap on total exchanges, no rate limit tied to time, and no per-account restriction preventing one funded account from repeatedly creating pairs across many blocks. The attack is entirely self-funded, repeatable, and does not require any validator/witness/committee cooperation, making it straightforward to execute given sufficient (but not necessarily large) capital and patience. The main uncertainty is the exact fee/exchange-balance-limit values in the currently configured `DynamicPropertiesStore` (`getExchangeBalanceLimit()`), which bound per-pair token amounts but not the pair count itself — this was not fully confirmed from the index but does not change the fundamental lack of an upper bound on `totalExchangeNum`.

### Recommendation
- Enforce a maximum on the total number of exchanges (`totalExchangeNum`) analogous to `Vault`'s recommended `totalMarkets` cap, rejecting new `ExchangeCreateContract` transactions once the limit is reached.
- Remove or gate the unpaginated `Wallet.getExchangeList()` / `listExchanges` / `ListExchangesServlet` (and PBFT/Solidity equivalents) behind the same limits already applied to `getPaginatedExchangeList`, or make `ExchangeStore.getAllExchanges()` itself pagination-aware/bounded (e.g., accept an offset/limit and never scan unboundedly).
- Alternatively, cap the response size in `Wallet.getExchangeList()` by internally delegating to the bounded pagination logic.

### Proof of Concept
1. An attacker-controlled account repeatedly issues `ExchangeCreateContract` transactions (each requiring only sufficient TRX/token balance to cover `calcFee()` and the chosen pair balances), incrementing `dynamicStore`'s exchange counter with no upper bound: [1](#0-0) 
2. After accumulating a very large number of exchange pairs, any anonymous client calls the gRPC `listExchanges` method or POSTs to `/wallet/listexchanges`.
3. This invokes `Wallet.getExchangeList()`, which calls `ExchangeStore.getAllExchanges()`, forcing the node to load and sort every exchange record and build a full protobuf list in memory: [2](#0-1) [3](#0-2) 
4. Repeated invocation of this endpoint by any client (or even a single large call) can exhaust node memory/CPU, denying the API to legitimate users, while `getPaginatedExchangeList`'s existing bound (`EXCHANGE_COUNT_LIMIT_MAX`) confirms the project is aware this exact pattern needs mitigation yet failed to apply it uniformly.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeCreateActuator.java (L78-119)
```java
      long id = addExact(dynamicStore.getLatestExchangeNum(), 1);
      long now = dynamicStore.getLatestBlockHeaderTimestamp();
      if (dynamicStore.getAllowSameTokenName() == 0) {
        //save to old asset store
        ExchangeCapsule exchangeCapsule =
            new ExchangeCapsule(
                exchangeCreateContract.getOwnerAddress(),
                id,
                now,
                firstTokenID,
                secondTokenID
            );
        exchangeCapsule.setBalance(firstTokenBalance, secondTokenBalance);
        exchangeStore.put(exchangeCapsule.createDbKey(), exchangeCapsule);

        //save to new asset store
        if (!Arrays.equals(firstTokenID, TRX_SYMBOL_BYTES)) {
          String firstTokenRealID = assetIssueStore.get(firstTokenID).getId();
          firstTokenID = firstTokenRealID.getBytes();
        }
        if (!Arrays.equals(secondTokenID, TRX_SYMBOL_BYTES)) {
          String secondTokenRealID = assetIssueStore.get(secondTokenID).getId();
          secondTokenID = secondTokenRealID.getBytes();
        }
      }

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

**File:** framework/src/main/java/org/tron/core/services/RpcApiService.java (L2032-2037)
```java
    @Override
    public void listExchanges(EmptyMessage request,
        StreamObserver<ExchangeList> responseObserver) {
      responseObserver.onNext(wallet.getExchangeList());
      responseObserver.onCompleted();
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
