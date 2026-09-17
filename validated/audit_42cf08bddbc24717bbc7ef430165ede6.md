### Title
Unbounded per-account delegation-index growth enables a resource-exhaustion DoS via `GetDelegatedResourceAccountIndex(V2)` — (File: `chainbase/src/main/java/org/tron/core/store/DelegatedResourceAccountIndexStore.java`)

### Summary
Any account that can broadcast `FreezeBalanceContract`/`DelegateResourceContract` transactions can create an unbounded number of `DelegatedResourceAccountIndex` DB entries keyed by `V2_FROM_PREFIX/owner/receiver` and `V2_TO_PREFIX/receiver/owner` [1](#0-0) . Neither the actuator/native-contract validators (`FreezeBalanceActuator.validate`, `FreezeBalanceProcessor.validate`) nor the store impose any cap on the number of distinct receiver/owner pairs an account may accumulate [2](#0-1) [3](#0-2) . When any client later queries `GetDelegatedResourceAccountIndex`/`GetDelegatedResourceAccountIndexV2` (HTTP servlets, gRPC `WalletGrpc`, and the Solidity/PBFT relays) for that same address, `getWithPrefix()` performs a full prefix scan over the store and materializes every matching row into an in-memory `List`, sorts it, and serializes the whole thing into the response with no page size limit [4](#0-3) . This is the same bug class as CVE-2024-4183: an unauthenticated/lightly-authenticated actor first floods a table with attacker-controlled rows, then triggers a query endpoint that has no result-size limit, causing unbounded memory/CPU consumption per request.

### Finding Description
- `FreezeBalanceActuator.execute()`/`FreezeBalanceProcessor.execute()` and `DelegateResourceActuator` call `DelegatedResourceAccountIndexStore.delegate()`/`delegateV2()`, which simply `put()`s a new key for every distinct `(owner, receiver)` pair [5](#0-4) .
- Validation only checks that `frozenBalance >= 1 TRX` and `frozenBalance <= accountBalance`; there is no limit on the number of unique receiver addresses a single owner can delegate to [2](#0-1) . An attacker can generate an arbitrary number of throwaway receiver addresses off-chain for free, and each delegation only costs 1 TRX (fully recoverable later via `UnfreezeBalance`), so growth of this index for a single address is cheap and effectively unbounded given enough transactions.
- The read path `getV2Index()`/`getIndex()` → `getWithPrefix()` does two `prefixQuery()` scans (one for the "from" prefix, one for the "to" prefix) over the underlying store, converts every matched capsule into an object, sorts the whole list by timestamp, and builds the full account list in memory before returning it — with no offset/limit parameters [6](#0-5) .
- This query is exposed unauthenticated via `GetDelegatedResourceAccountIndexServlet`/`GetDelegatedResourceAccountIndexV2Servlet` (HTTP), `WalletGrpc.getDelegatedResourceAccountIndex(V2)` (gRPC), and the Solidity/PBFT relay servlets that simply forward to these [7](#0-6) [8](#0-7) [9](#0-8) .
- Notably, sibling list APIs in the same `Wallet` class (`getPaginatedExchangeList`, `getAssetIssueList`/`GetPaginatedAssetIssueListServlet`) explicitly cap results with `offset`/`limit` and `EXCHANGE_COUNT_LIMIT_MAX` [10](#0-9) , showing the delegated-resource-index endpoints are the outlier without any such protection.

### Impact Explanation
Each request against a "poisoned" address forces the node to perform two unbounded prefix scans, allocate and sort large in-memory lists, and serialize a potentially very large protobuf/JSON response. Since anyone can pre-populate a large number of index rows against their own address (or, since the "to"-prefix side is populated by the *sender*, against a victim address they choose as `receiver`), an attacker can create an index blowup on a target address without the target's participation, then repeatedly query that address via the public HTTP/gRPC wallet API to consume node memory/CPU/network bandwidth on every FullNode, SolidityNode, or PBFT node that serves this endpoint. Repeated concurrent requests against a heavily poisoned address can exhaust heap or degrade query-serving threads, denying legitimate API service — the same availability impact class as the referenced Mattermost `getSessions` issue (CWE-400/770).

### Likelihood Explanation
- The write path is fully reachable by any funded account via a standard, cheap transaction (`FreezeBalanceContract`/`DelegateResourceContract`), requiring only 1 TRX per new receiver entry, which is refundable after unfreezing.
- The read path is a public, unauthenticated query API present on FullNode/SolidityNode/PBFT HTTP and gRPC surfaces.
- No pagination, offset/limit, or maximum-index-size enforcement exists anywhere in the write or read path, unlike comparable list APIs (`GetPaginatedExchangeList`, `GetPaginatedAssetIssueList`) that already implement such caps.
- The main mitigating factor is that populating a very large index requires many separate freeze/delegate transactions (each subject to normal bandwidth/energy accounting and TRX escrow), so a full-scale attack takes sustained transaction volume rather than a single request; this differs from the original Mattermost bug where session creation was essentially free per login attempt.

### Recommendation
- Add an explicit cap on the number of distinct delegation index entries per address (analogous to `frozenCount` checks) enforced in `FreezeBalanceActuator`/`FreezeBalanceProcessor`/`DelegateResourceActuator` validation.
- Add `offset`/`limit` parameters (mirroring `GetPaginatedExchangeList`/`GetPaginatedAssetIssueList`) to `getDelegatedResourceAccountIndex`/`getDelegatedResourceAccountIndexV2` in `Wallet.java`, and thread them through `getWithPrefix()` in `DelegatedResourceAccountIndexStore` so `prefixQuery()` results are limited before being materialized/sorted.
- Apply a strict rate limit (e.g., `rate.limiter` `IPQPSRateLimiterAdapter`) specifically to the delegated-resource-index servlets/gRPC methods as a defense-in-depth measure.

### Proof of Concept
1. Fund account `A` with enough TRX to send N freeze/delegate transactions (each escrowing 1 TRX, refundable later).
2. For `i = 1..N`, generate a fresh throwaway address `R_i` and broadcast `FreezeBalanceContract`/`DelegateResourceContract` from `A` to `R_i` for 1 TRX — each call inserts a new row via `DelegatedResourceAccountIndexStore.delegateV2()` [1](#0-0) .
3. After N is large (e.g., tens of thousands), repeatedly call `GET /wallet/getdelegatedresourceaccountindexv2?value=<A>` (or the gRPC equivalent) concurrently from multiple clients.
4. Each request triggers `getV2Index()` → `getWithPrefix()`, which performs unbounded `prefixQuery()` scans, builds and sorts full in-memory lists, then serializes them [6](#0-5) , consuming disproportionate CPU/memory per request and degrading or crashing the serving node under sustained concurrent load — mirroring the `getSessions` flood-then-crash pattern in the referenced advisory.

### Citations

**File:** chainbase/src/main/java/org/tron/core/store/DelegatedResourceAccountIndexStore.java (L63-89)
```java
  public void delegate(byte[] from, byte[] to, long time) {
    byte[] fromKey = Bytes.concat(FROM_PREFIX, from, to);
    DelegatedResourceAccountIndexCapsule toIndexCapsule =
        new DelegatedResourceAccountIndexCapsule(ByteString.copyFrom(to));
    toIndexCapsule.setTimestamp(time);
    this.put(fromKey, toIndexCapsule);

    byte[] toKey = Bytes.concat(TO_PREFIX, to, from);
    DelegatedResourceAccountIndexCapsule fromIndexCapsule =
        new DelegatedResourceAccountIndexCapsule(ByteString.copyFrom(from));
    fromIndexCapsule.setTimestamp(time);
    this.put(toKey, fromIndexCapsule);
  }

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

**File:** actuator/src/main/java/org/tron/core/actuator/FreezeBalanceActuator.java (L187-201)
```java
    long frozenBalance = freezeBalanceContract.getFrozenBalance();
    if (frozenBalance <= 0) {
      throw new ContractValidateException("frozenBalance must be positive");
    }
    if (frozenBalance < TRX_PRECISION) {
      throw new ContractValidateException("frozenBalance must be greater than or equal to 1 TRX");
    }

    int frozenCount = accountCapsule.getFrozenCount();
    if (!(frozenCount == 0 || frozenCount == 1)) {
      throw new ContractValidateException("frozenCount must be 0 or 1");
    }
    if (frozenBalance > accountCapsule.getBalance()) {
      throw new ContractValidateException("frozenBalance must be less than or equal to accountBalance");
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/FreezeBalanceProcessor.java (L26-42)
```java
    // validate arg @frozenBalance
    byte[] ownerAddress = param.getOwnerAddress();
    AccountCapsule ownerCapsule = repo.getAccount(ownerAddress);
    long frozenBalance = param.getFrozenBalance();
    if (frozenBalance <= 0) {
      throw new ContractValidateException("FrozenBalance must be positive");
    } else if (frozenBalance < TRX_PRECISION) {
      throw new ContractValidateException("FrozenBalance must be greater than or equal to 1 TRX");
    } else if (frozenBalance > ownerCapsule.getBalance()) {
      throw new ContractValidateException("FrozenBalance must be less than or equal to accountBalance");
    }

    // validate frozen count of owner account
    int frozenCount = ownerCapsule.getFrozenCount();
    if (frozenCount != 0 && frozenCount != 1) {
      throw new ContractValidateException("FrozenCount must be 0 or 1");
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

**File:** framework/src/main/java/org/tron/core/services/RpcApiService.java (L528-550)
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

    @Override
    public void getDelegatedResourceAccountIndexV2(BytesMessage request,
        StreamObserver<org.tron.protos.Protocol.DelegatedResourceAccountIndex> responseObserver) {
      try {
        responseObserver
                .onNext(wallet.getDelegatedResourceAccountIndexV2(request.getValue()));
      } catch (Exception e) {
        responseObserver.onError(getRunTimeException(e));
      }
      responseObserver.onCompleted();
    }
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
