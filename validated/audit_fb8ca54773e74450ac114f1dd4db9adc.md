## Analog Found

### Title
Unbounded per-address delegation index growth enables a griefing DoS against `getDelegatedResourceAccountIndex(V2)` query path - (File: `chainbase/src/main/java/org/tron/core/store/DelegatedResourceAccountIndexStore.java`)

### Summary
The original OpenQ report describes a griefing attack where an attacker inflates an unbounded list tied to a shared resource (bounty deposits) with many cheap/dust entries, so that a legitimate user's later operation, which must loop over the entire list, becomes prohibitively expensive or reverts from gas exhaustion. In java-tron, an analogous unbounded, attacker-inflatable list exists in `DelegatedResourceAccountIndexStore`, which backs the `getDelegatedResourceAccountIndex`/`getDelegatedResourceAccountIndexV2` API.

### Finding Description
`DelegateResourceActuator.delegateResource()` calls `delegatedResourceAccountIndexStore.delegateV2(ownerAddress, receiverAddress, ...)` on every successful `DelegateResourceContract` execution: [1](#0-0) 

`delegateV2` writes a brand-new DB entry keyed by the concatenation of `V2_TO_PREFIX + to + from`, so each distinct `from` (owner) address delegating to the same `to` (receiver) address creates a permanent, distinct key that is never merged/capped: [2](#0-1) 

The only cost gate for a `DelegateResourceContract` is a floor of 1 TRX (`delegateBalance < TRX_PRECISION` is rejected), which is cheap and can be sourced from disposable throwaway accounts each freezing the minimum amount via `FreezeBalanceV2Contract` before delegating: [3](#0-2) 

When any client queries the index for the targeted `receiver` address, `getV2Index()`/`getWithPrefix()` performs an unbounded `prefixQuery` over all keys with that prefix, materializes every matching entry into a list, and sorts it — with no upper bound on the number of entries scanned or returned: [4](#0-3) 

This query path is reachable by any anonymous API client through gRPC and HTTP: [5](#0-4) [6](#0-5) [7](#0-6) 

An attacker can create an arbitrary number of throwaway accounts, freeze the 1-TRX minimum in each, and issue `DelegateResourceContract` transactions targeting the same victim `receiverAddress`. Each such transaction is cheap and permanently inserts a new prefixed key under that victim's index. There is no limit analogous to `LogBlockQuery.MAX_RESULT` (used elsewhere in the codebase, e.g. `LogMatch.matchBlockOneByOne()`) to bound the number of scanned/returned entries here.

### Impact Explanation
Because the number of entries scanned by `getWithPrefix` grows linearly with attacker-created delegation records that cost only the 1 TRX minimum per transaction, an attacker can make queries against a targeted address's delegated-resource index arbitrarily expensive in CPU, memory, and response-payload size. Since this endpoint is served on the public gRPC/HTTP API without a result cap, sustained attack traffic can degrade or effectively deny this query path for any node serving it, which the validation rules classify as "an API the node can no longer serve." This mirrors the underlying grief-DoS class from the source report: cheap, permanent, unbounded-list inflation that a third party leverages to block a legitimate consumer's read of their own resource-index data.

### Likelihood Explanation
Likelihood is moderate: the attack requires only unprivileged account creation, cheap 1-TRX freezes, and repeated low-fee `DelegateResourceContract` transactions—no special privileges, and the write path (`delegateV2`) never rejects or caps growth per receiver address.

### Recommendation
Bound `getWithPrefix`'s `prefixQuery` result count (e.g., cap the number of entries returned/scanned similar to `LogBlockQuery.MAX_RESULT`), and/or enforce a maximum number of distinct delegation records permitted per `(receiver)` or `(owner, receiver)` pair, merging repeat delegations from newly created addresses instead of always creating new keys.

### Proof of Concept
1. Create `N` throwaway accounts; for each, send `FreezeBalanceV2Contract` freezing the minimum bandwidth/energy balance.
2. From each throwaway account, send `DelegateResourceContract` with `balance = 1 TRX` targeting the same victim `receiverAddress`. Each call passes validation (`delegateBalance >= TRX_PRECISION`) and inserts a new key `V2_TO_PREFIX + receiver + fromAddress_i` via `delegateV2` [2](#0-1) .
3. Repeat for large `N` (limited only by the attacker's willingness to freeze 1 TRX per throwaway account, recoverable later via unfreeze).
4. Any client calling `getDelegatedResourceAccountIndexV2(receiverAddress)` (HTTP `/wallet/getdelegatedresourceaccountindexv2` or gRPC) triggers `getWithPrefix`, which now must `prefixQuery`, deserialize, and sort `N` entries [8](#0-7) , degrading that query for the victim and other legitimate callers.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/DelegateResourceActuator.java (L147-150)
```java
    long delegateBalance = delegateResourceContract.getBalance();
    if (delegateBalance < TRX_PRECISION) {
      throw new ContractValidateException("delegateBalance must be greater than or equal to 1 TRX");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/DelegateResourceActuator.java (L313-315)
```java
    //modify DelegatedResourceAccountIndexStore
    delegatedResourceAccountIndexStore.delegateV2(ownerAddress, receiverAddress,
        dynamicPropertiesStore.getLatestBlockHeaderTimestamp());
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

**File:** framework/src/main/java/org/tron/core/services/http/GetDelegatedResourceAccountIndexV2Servlet.java (L24-35)
```java
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
```

**File:** framework/src/main/java/org/tron/core/services/RpcApiService.java (L515-526)
```java
    @Override
    public void getDelegatedResourceV2(DelegatedResourceMessage request,
        StreamObserver<DelegatedResourceList> responseObserver) {
      try {
        responseObserver.onNext(wallet.getDelegatedResourceV2(
                request.getFromAddress(), request.getToAddress())
        );
      } catch (Exception e) {
        responseObserver.onError(getRunTimeException(e));
      }
      responseObserver.onCompleted();
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
