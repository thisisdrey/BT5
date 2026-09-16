### Title
Unbounded delegated-resource index growth enables DoS on `GetDelegatedResourceAccountIndexV2` query path - (File: `chainbase/src/main/java/org/tron/core/store/DelegatedResourceAccountIndexStore.java`)

### Summary
`DelegatedResourceAccountIndexStore` stores, per `(from, to)` pair, a separate DB key under `V2_FROM_PREFIX`/`V2_TO_PREFIX`. Any account can call `DelegateResourceContract` to delegate as little as 1 TRX of bandwidth/energy to an arbitrary `receiverAddress`, which appends a new index entry keyed by that unique `(owner, receiver)` pair. By using many distinct `owner` accounts to delegate to the same victim `receiver`, an attacker can grow the victim's reverse index (`V2_TO_PREFIX + receiver`) without bound, at low cost (the delegated TRX is not spent, only frozen and later recoverable). When the resulting index is queried via `getDelegatedResourceAccountIndexV2`/`getV2Index`, the store performs an unbounded `prefixQuery` over all matching keys, materializes them into an `ArrayList`, and sorts the full list — with no pagination or cap.

### Finding Description
- `DelegateResourceActuator.delegateResource()` calls `delegatedResourceAccountIndexStore.delegateV2(ownerAddress, receiverAddress, timestamp)`, which writes two DB entries keyed by `Bytes.concat(V2_FROM_PREFIX, from, to)` and `Bytes.concat(V2_TO_PREFIX, to, from)`. [1](#0-0) [2](#0-1) 
- Validation only requires `delegateBalance >= 1 TRX_PRECISION` and a valid, non-contract receiver address; there is no cap on how many distinct owners may delegate to the same receiver, nor any minimum cost tied to creating a new index entry beyond a single 1 TRX freeze (which the attacker can later reclaim via unfreeze/undelegate). [3](#0-2) 
- Reading back the index (`getV2Index` → `getWithPrefix`) does an unbounded `prefixQuery` for both `V2_FROM_PREFIX+address` and `V2_TO_PREFIX+address`, converts all matches to a `List`, and sorts by timestamp — no limit, offset, or pagination. [4](#0-3) 
- This is exposed unauthenticated via the HTTP servlet `GetDelegatedResourceAccountIndexV2Servlet`, which simply forwards any caller-supplied address to `wallet.getDelegatedResourceAccountIndexV2(address)` and serializes the full result to JSON. [5](#0-4) 

### Impact Explanation
An attacker can inflate the reverse index of any account (their own address, an exchange hot wallet, a victim's address, etc.) to an arbitrarily large size by broadcasting many cheap `DelegateResourceContract` transactions from many funded accounts. Every subsequent call to the `GetDelegatedResourceAccountIndexV2` (or `V1`) query API for that address forces the node to run an unbounded DB prefix scan, build a large in-memory list, sort it, and serialize potentially huge JSON responses. Because the endpoint is reachable via unauthenticated HTTP/gRPC without any pagination, this can be repeated to consume node CPU/memory on every full node, solidity node, and PBFT node serving this API, degrading or crashing the query-serving path (API-availability DoS) for that address.

### Likelihood Explanation
Likelihood is moderate-to-high: creating each index entry only requires minimal TRX (1 TRX per delegation, refundable), a distinct account address, and a normal, permissionless `DelegateResourceContract` transaction — no special privilege, no witness/SR status required. The attack is purely economic (cost of account creation/transaction fees) and can be automated to add a large number of entries over time.

### Recommendation
Add pagination/limits to `getWithPrefix`/`getV2Index` (e.g., cap prefix scan size, support limit/offset parameters in the servlets and Wallet methods) and/or bound the number of distinct delegation index entries per receiver/owner pair enforced at actuator validation time, rejecting or rate-limiting excessive fan-out of new `(from,to)` pairs.

### Proof of Concept
1. Attacker creates N funded accounts (each needs ≥1 TRX plus enough for a `FreezeBalanceV2Contract`).
2. Each account calls `freezeBalanceV2` to obtain 1 TRX of bandwidth, then calls `DelegateResourceContract` with `receiverAddress = victim`, `balance = 1 TRX_PRECISION`. This is accepted per the validation logic shown above. [6](#0-5) 
3. Repeating this for a large N grows the `V2_TO_PREFIX + victim` key space in `DelegatedResourceAccountIndexStore` linearly with N.
4. A caller then invokes `GET /wallet/getdelegatedresourceaccountindexv2?value=<victim>`, triggering `getWithPrefix` to scan and sort all N entries, and return the full JSON list. [7](#0-6) 
5. Repeated calls to this endpoint by the attacker (or naturally by clients/exchanges checking the victim's delegations) impose growing CPU/memory cost on every node serving the API.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/DelegateResourceActuator.java (L140-150)
```java
    AccountCapsule ownerCapsule = accountStore.get(ownerAddress);
    if (ownerCapsule == null) {
      String readableOwnerAddress = StringUtil.createReadableString(ownerAddress);
      throw new ContractValidateException(
          ActuatorConstant.ACCOUNT_EXCEPTION_STR + readableOwnerAddress + NOT_EXIST_STR);
    }

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

**File:** framework/src/main/java/org/tron/core/services/http/GetDelegatedResourceAccountIndexV2Servlet.java (L60-69)
```java
  private void fillResponse(ByteString address, boolean visible, HttpServletResponse response)
      throws IOException {
    DelegatedResourceAccountIndex reply =
        wallet.getDelegatedResourceAccountIndexV2(address);
    if (reply != null) {
      response.getWriter().println(JsonFormat.printToString(reply, visible));
    } else {
      response.getWriter().println("{}");
    }
  }
```
