## Title
Unbounded per-account delegation index growth enables Denial of Service via `GetDelegatedResourceAccountIndex(V2)` - (File: `chainbase/src/main/java/org/tron/core/store/DelegatedResourceAccountIndexStore.java`)

### Summary
The Discourse CVE stems from users being able to create an unlimited number of unbounded-size chat drafts, which are then fully loaded (with no pagination/limit) whenever the user's data is fetched, causing resource exhaustion. java-tron has a structurally identical pattern in its resource-delegation index: any account can, through ordinary `DelegateResourceContract` transactions, create an unbounded number of persisted delegation-index entries, and the node fully materializes *all* of them, unbounded, whenever anyone queries that account through the public `getdelegatedresourceaccountindex`/`getdelegatedresourceaccountindexv2` HTTP or gRPC endpoints.

### Finding Description
When an account delegates frozen resources (bandwidth/energy) to another account via `DelegateResourceActuator`, the actuator persists a delegation-index entry keyed by `V2_FROM_PREFIX + owner + receiver` and `V2_TO_PREFIX + receiver + owner`: [1](#0-0) 

There is no cap on how many distinct receiver addresses one owner can delegate to, nor on how many distinct owners can delegate to one receiver — the only requirement per delegation is a minimum of 1 TRX of frozen balance to delegate, which is not burned and can be reused (undelegated and re-delegated) or split across arbitrarily many freshly created receiver accounts: [2](#0-1) 

When any client later queries the delegation index for an address (an unauthenticated, publicly reachable read path), the store performs an unbounded `prefixQuery` over *all* entries under that address's `FROM`/`TO` prefixes, loads every result into memory, sorts it, and builds full `toAccounts`/`fromAccounts` lists with no size limit: [3](#0-2) 

This is reachable via the public HTTP servlets: [4](#0-3) 
and via `Wallet.getDelegatedResourceAccountIndexV2`, which calls straight through to the unbounded `getV2Index`: [5](#0-4) 
as well as the equivalent gRPC RPCs `GetDelegatedResourceAccountIndex`/`GetDelegatedResourceAccountIndexV2`: [6](#0-5) 

This mirrors the Discourse bug class exactly: (1) an unprivileged actor can create an unbounded number of "records" attached to an account (analogous to unlimited chat drafts) via ordinary signed transactions, and (2) a single subsequent read/query operation on that account loads *all* of them without any limit, imposing unbounded CPU/memory/I/O cost on the node servicing that one request.

### Impact Explanation
An attacker can cheaply generate tens of thousands (or more) of delegation-index entries under a single address by repeatedly issuing `DelegateResourceContract` transactions to many distinct receiver addresses (each requiring only 1 TRX delegated, which remains attacker-owned and can be recycled via undelegate/delegate cycles). Any subsequent call to `getdelegatedresourceaccountindex(v2)` for that address — which any unauthenticated API client can issue, including the node's own internal callers — forces the full-node/solidity-node/PBFT-node process to scan and materialize the entire unbounded key range, sort it, and serialize it into a JSON/protobuf response. Repeated or concurrent requests against a bloated address can exhaust node memory/CPU, degrading or crashing the HTTP/gRPC API service and potentially destabilizing the node process that also participates in block application — an availability impact on the node itself and on any consumer relying on this query (wallets, exchanges).

### Likelihood Explanation
The precondition (creating the bloated index) requires only ordinary, unprivileged `DelegateResourceContract` transactions and modest TRX to freeze — no special privileges, no consensus role, and no validation gap needs to be exploited beyond the complete absence of a maximum-entries check on the delegation index. The trigger (the query) is a single unauthenticated HTTP GET/POST or gRPC call. This makes the attack straightforward and repeatable by any transaction broadcaster or anonymous API client.

### Recommendation
Introduce a maximum bound on the number of delegation-index entries an account can accumulate (reject further `DelegateResourceContract` calls once the cap is reached, similar to how other unbounded string fields such as asset name/url/description are capped in `TransactionUtil`), and/or paginate `getWithPrefix`/`prefixQuery` results in `DelegatedResourceAccountIndexStore` so that the `GetDelegatedResourceAccountIndex(V2)` read paths cannot be forced to materialize an unbounded number of records in a single request.

### Proof of Concept
1. Attacker account A freezes N TRX for bandwidth/energy (`FreezeBalanceV2Contract`), enabling delegation.
2. Attacker creates M throwaway receiver accounts R1..RM (cheap, minimal TRX to activate each).
3. Attacker issues M separate `DelegateResourceContract` transactions from A to R1..RM, each delegating the minimum 1 TRX — each call adds a `V2_FROM_PREFIX+A+Ri` / `V2_TO_PREFIX+Ri+A` entry per `DelegateResourceActuator.delegateResource` / `DelegatedResourceAccountIndexStore.delegateV2` (`actuator/src/main/java/org/tron/core/actuator/DelegateResourceActuator.java:313-315`, `chainbase/src/main/java/org/tron/core/store/DelegatedResourceAccountIndexStore.java:77-89`).
4. Any client calls `GET /wallet/getdelegatedresourceaccountindexv2?value=<A>` (or the gRPC `GetDelegatedResourceAccountIndexV2`), which invokes `DelegatedResourceAccountIndexStore.getWithPrefix` — this executes an unbounded `prefixQuery` over all M entries, sorts them, and returns them all in one response (`chainbase/src/main/java/org/tron/core/store/DelegatedResourceAccountIndexStore.java:118-138`), consuming CPU/memory proportional to M for a single unauthenticated request. Increasing M (arbitrarily large, since there's no cap) increases the per-request cost without bound.

### Citations

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

**File:** chainbase/src/main/java/org/tron/core/store/DelegatedResourceAccountIndexStore.java (L106-138)
```java
  public DelegatedResourceAccountIndexCapsule getIndex(byte[] address) {
    DelegatedResourceAccountIndexCapsule indexCapsule = get(address);
    if (indexCapsule != null) {
      return indexCapsule;
    }
    return getWithPrefix(FROM_PREFIX, TO_PREFIX, address);
  }

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

**File:** actuator/src/main/java/org/tron/core/actuator/DelegateResourceActuator.java (L146-150)
```java

    long delegateBalance = delegateResourceContract.getBalance();
    if (delegateBalance < TRX_PRECISION) {
      throw new ContractValidateException("delegateBalance must be greater than or equal to 1 TRX");
    }
```

**File:** framework/src/main/java/org/tron/core/services/http/GetDelegatedResourceAccountIndexV2Servlet.java (L17-37)
```java
@Component
@Slf4j(topic = "API")
public class GetDelegatedResourceAccountIndexV2Servlet extends RateLimiterServlet {

  private static final  String VALUE_FIELD_NAME = "value";

  @Autowired
  private Wallet wallet;

  protected void doGet(HttpServletRequest request, HttpServletResponse response) {
    try {
      boolean visible = Util.getVisible(request);
      String address = request.getParameter(VALUE_FIELD_NAME);
      if (visible) {
        address = Util.getHexAddress(address);
      }
      fillResponse(ByteString.copyFrom(ByteArray.fromHexString(address)), visible, response);
    } catch (Exception e) {
      Util.processError(e, response);
    }
  }
```

**File:** framework/src/main/java/org/tron/core/Wallet.java (L1053-1064)
```java
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
