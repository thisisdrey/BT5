## Title
Unbounded `DelegatedResourceAccountIndex` prefix scan allows unbounded-size API responses / node resource exhaustion - (File: `chainbase/src/main/java/org/tron/core/store/DelegatedResourceAccountIndexStore.java`)

### Summary
The reported Discourse bug is caused by a message serializer that embeds the *fully expanded* list of mentioned users (`@all`/`@here`) into a single message without any bound, letting the list grow unboundedly and blow up serialization cost/size. The analogous pattern exists in java-tron's `DelegatedResourceAccountIndexStore.getWithPrefix()`, which scans **all** `from`/`to` delegation records for an address via `prefixQuery` and stuffs them into a single `DelegatedResourceAccountIndexCapsule`/`DelegatedResourceAccountIndex` protobuf message with no cap on the number of entries.

### Finding Description
`DelegatedResourceAccountIndexStore.getWithPrefix()` performs an unbounded key-prefix scan and materializes every match into in-memory lists before returning a single capsule: [1](#0-0) 

This is invoked by `getIndex()`/`getV2Index()`, which are exposed directly to unauthenticated/unprivileged callers through `Wallet.getDelegatedResourceAccountIndex()` / `getDelegatedResourceAccountIndexV2()`: [2](#0-1) 

Those Wallet methods are reachable via:
- HTTP: `GetDelegatedResourceAccountIndexServlet` / `GetDelegatedResourceAccountIndexV2Servlet` (and their Solidity-node counterparts) [3](#0-2) 
- gRPC: `RpcApiService.getDelegatedResourceAccountIndex(V2)` [4](#0-3) 

None of these API layers apply any offset/limit — unlike `getPaginatedProposalList`/`getPaginatedExchangeList`, which explicitly cap results via `PROPOSAL_COUNT_LIMIT_MAX`/`EXCHANGE_COUNT_LIMIT_MAX`: [5](#0-4) 

The number of delegation entries an attacker can create for a single address is unbounded: `DelegateResourceActuator.validate()` only enforces a minimum of 1 TRX per delegation (`delegateBalance >= TRX_PRECISION`), with no maximum count of distinct receivers per owner (or vice-versa, no cap on distinct delegators to one receiver): [6](#0-5) 

Each successful `DelegateResourceContract` (or the equivalent TVM native contract path in `DelegateResourceProcessor`) writes a new `from`/`to` index entry: [7](#0-6) 

An attacker who controls (or funds) many receiver accounts can repeatedly freeze the 1-TRX minimum and delegate it to a fresh receiver address each time, accumulating an arbitrarily large `toAccounts`/`fromAccounts` list tied to one owner address. Any unprivileged client can then query `GetDelegatedResourceAccountIndex(V2)` for that address, forcing the node to run the unbounded prefix scan and serialize the entire (attacker-controlled-size) list into a single response, with no size limit enforced anywhere in this path.

### Impact Explanation
Because there is no limit on the number of delegation index entries returned per query, repeated calls to `GetDelegatedResourceAccountIndex`/`V2` against an address with a very large delegation fan-out can force the serving node (FullNode, SolidityNode, or PBFT node) to allocate large in-memory lists and build/serialize an unbounded protobuf response on every request. Combined with the ability of any unprivileged account to grow this fan-out cheaply and repeatedly, this can be used to degrade or crash the node process handling this specific query path (memory/CPU exhaustion), i.e. "an API the node can no longer serve."

### Likelihood Explanation
The prerequisite delegation cost is low (1 TRX minimum per delegate call plus existence of receiver accounts), and the query endpoints (`GetDelegatedResourceAccountIndexServlet`, its V2/Solidity variants, and the gRPC equivalents) are unauthenticated and reachable by any external caller. No account activation or special privilege is required beyond ordinary `DelegateResourceContract` broadcasting, which any funded account can do.

### Recommendation
Add pagination/limit parameters to `DelegatedResourceAccountIndexStore.getWithPrefix()` (and its callers `getIndex`/`getV2Index`), analogous to the existing `getPaginatedProposalList`/`getPaginatedExchangeList` patterns, and enforce a maximum entry count (and/or maximum serialized response size) before returning results through the HTTP/gRPC/JSON-RPC layers.

### Proof of Concept
1. Attacker funds account `A` with `N` TRX and creates/uses `N` distinct receiver addresses `R_1..R_N`.
2. Attacker freezes 1 TRX at a time for bandwidth/energy and broadcasts `N` separate `DelegateResourceContract` transactions from `A` to each `R_i` (each satisfies the `>= 1 TRX` validation check in `DelegateResourceActuator.validate()`).
3. Each transaction appends an entry into the `V2_FROM_PREFIX`/`V2_TO_PREFIX` index for `A` via `DelegateResourceProcessor.delegateResource()`.
4. Any unauthenticated client calls `GET /wallet/getdelegatedresourceaccountindexv2?value=<A>` (or the gRPC `GetDelegatedResourceAccountIndexV2`), triggering `DelegatedResourceAccountIndexStore.getWithPrefix()` to scan and serialize all `N` entries with no cap, repeatable by anyone at will to amplify load on the serving node.

### Citations

**File:** chainbase/src/main/java/org/tron/core/store/DelegatedResourceAccountIndexStore.java (L118-137)
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
```

**File:** framework/src/main/java/org/tron/core/Wallet.java (L1040-1064)
```java
  public DelegatedResourceAccountIndex getDelegatedResourceAccountIndex(ByteString address) {
    if (address == null || address.size() != DecodeUtil.ADDRESS_SIZE / 2) {
      return DelegatedResourceAccountIndex.getDefaultInstance();
    }
    DelegatedResourceAccountIndexCapsule accountIndexCapsule =
        chainBaseManager.getDelegatedResourceAccountIndexStore().getIndex(address.toByteArray());
    if (accountIndexCapsule != null) {
      return accountIndexCapsule.getInstance();
    } else {
      return DelegatedResourceAccountIndex.getDefaultInstance();
    }
  }

  public DelegatedResourceAccountIndex getDelegatedResourceAccountIndexV2(ByteString address) {
    if (address == null || address.size() != DecodeUtil.ADDRESS_SIZE / 2) {
      return DelegatedResourceAccountIndex.getDefaultInstance();
    }
    DelegatedResourceAccountIndexCapsule accountIndexCapsule = chainBaseManager
        .getDelegatedResourceAccountIndexStore().getV2Index(address.toByteArray());
    if (accountIndexCapsule != null) {
      return accountIndexCapsule.getInstance();
    } else {
      return DelegatedResourceAccountIndex.getDefaultInstance();
    }
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

**File:** framework/src/main/java/org/tron/core/services/http/GetDelegatedResourceAccountIndexServlet.java (L24-35)
```java
  protected void doGet(HttpServletRequest request, HttpServletResponse response) {
    try {
      boolean visible = Util.getVisible(request);
      String address = request.getParameter("value");
      if (visible) {
        address = Util.getHexAddress(address);
      }
      fillResponse(ByteString.copyFrom(ByteArray.fromHexString(address)), visible, response);
    } catch (Exception e) {
      Util.processError(e, response);
    }
  }
```

**File:** framework/src/main/java/org/tron/core/services/RpcApiService.java (L528-538)
```java
    @Override
    public void getDelegatedResourceAccountIndex(BytesMessage request,
        StreamObserver<org.tron.protos.Protocol.DelegatedResourceAccountIndex> responseObserver) {
      try {
        responseObserver
          .onNext(wallet.getDelegatedResourceAccountIndex(request.getValue()));
      } catch (Exception e) {
        responseObserver.onError(getRunTimeException(e));
      }
      responseObserver.onCompleted();
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/DelegateResourceActuator.java (L147-150)
```java
    long delegateBalance = delegateResourceContract.getBalance();
    if (delegateBalance < TRX_PRECISION) {
      throw new ContractValidateException("delegateBalance must be greater than or equal to 1 TRX");
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
