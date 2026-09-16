### Title
Unbounded growth of DelegatedResourceAccountIndex per-account prefix list allows DoS of GetDelegatedResourceAccountIndexV2 query API - (File: chainbase/src/main/java/org/tron/core/store/DelegatedResourceAccountIndexStore.java)

### Summary
`DelegatedResourceAccountIndexStore.getWithPrefix` performs a full `prefixQuery` scan over all `V2_FROM_PREFIX`/`V2_TO_PREFIX` keyed entries for a given address, loads them all into memory, sorts them, and returns them as a `DelegatedResourceAccountIndex` list. [1](#0-0)  This is directly analogous to the reported `CollateralManager.getCollateralInfo` issue: an unbounded, attacker-growable collection is fully materialized on every read, and the mechanism that grows the collection has no cap on the number of distinct entries an attacker can create against a single victim address.

### Finding Description
`DelegateResourceActuator` lets any account delegate as little as 1 TRX of frozen bandwidth/energy to any receiver address, with no limit on the number of distinct delegation relationships that can be created. [2](#0-1)  Each successful delegation writes a new `V2_FROM_PREFIX`/`V2_TO_PREFIX` keyed entry via `DelegatedResourceAccountIndexStore.delegateV2` [3](#0-2) , keyed by `prefix+from+to` (or `prefix+to+from`), so the number of entries per victim address is unbounded — an attacker can generate arbitrarily many new "from" accounts (each only needs 1 TRX frozen) and delegate to the same receiver, or receive delegations from arbitrarily many accounts.

When `getDelegatedResourceAccountIndexV2` is queried (via gRPC `GetDelegatedResourceAccountIndexV2`, HTTP `/wallet/getdelegatedresourceaccountindexv2`, and their Solidity/PBFT node equivalents), `Wallet.getDelegatedResourceAccountIndexV2` calls `DelegatedResourceAccountIndexStore.getV2Index`, which calls `getWithPrefix`. [4](#0-3)  `getWithPrefix` runs `this.prefixQuery(key)` twice (once for the "to" list, once for the "from" list), materializes every matching key/value pair into an `ArrayList`, sorts the full list by timestamp, and maps it into `ByteString` accounts before returning. [1](#0-0)  None of the RPC/HTTP/Solidity handlers cap or paginate the result. [5](#0-4) [6](#0-5) 

This means:
1. Any unprivileged account (broadcaster of a signed `DelegateResourceContract` transaction) can pick a victim address and delegate to/from it many times using many different, cheaply generated key pairs (only 1 TRX of frozen balance needed each time, which can be re-frozen and reused/moved between multiple attacker-controlled accounts).
2. Each such delegation is a distinct on-chain transaction that costs the attacker only bandwidth/energy fees, growing the victim's `DelegatedResourceAccountIndex` prefix set without bound.
3. Any subsequent call to `GetDelegatedResourceAccountIndexV2`/HTTP `getdelegatedresourceaccountindexv2` for that victim address triggers a full prefix scan, in-memory materialization, and sort proportional to the number of entries created — an unauthenticated, publicly reachable read that can be made arbitrarily expensive.

### Impact Explanation
This does not directly steal funds, but it degrades a public query API (`GetDelegatedResourceAccountIndexV2` gRPC/HTTP/Solidity/PBFT endpoints) that any external client depends on, turning a targeted victim address's index query into an expensive/slow operation, and can be used to consume node CPU/memory resources when repeatedly queried, satisfying the "API the node can no longer serve" bar for a Medium-severity DoS analog. The severity is bounded because it targets a query path, not consensus-critical block processing, and requires many attacker transactions (cost scales with target list size), but there is no mitigating cap on the number of index entries per account in the codebase.

### Likelihood Explanation
Likelihood is moderate: it requires an attacker to spend TRX fees for many small `DelegateResourceContract` transactions (1 TRX minimum lock each, reusable after unlock/expiry), which is inexpensive relative to potential service degradation, and the resulting DoS is only realized when someone queries the poisoned address's index — but that query path is a standard, commonly used public API (wallet/exchange balance-delegation tooling), so it is very likely to eventually be hit.

### Recommendation
Introduce a hard cap on the number of distinct delegation relationships (`from`/`to` accounts) tracked per address in `DelegatedResourceAccountIndexStore`, or migrate `getDelegatedResourceAccountIndexV2`/`GetDelegatedResourceAccountIndexV2` (and HTTP/Solidity/PBFT equivalents) to a paginated query similar to `GetPaginatedAssetIssueList`/`GetPaginatedNowWitnessList`, so that a single query cannot be forced to scan and materialize an unbounded number of entries.

### Proof of Concept
1. Attacker generates N throwaway accounts, each activated and freezing the minimum 1 TRX via `FreezeBalanceV2Contract`.
2. For each account, attacker broadcasts a `DelegateResourceContract` transaction delegating 1 TRX (`TRX_PRECISION`) of BANDWIDTH or ENERGY to the same victim `receiverAddress`, satisfying the only balance check in `DelegateResourceActuator.validate` (`delegateBalance >= TRX_PRECISION`). [2](#0-1) 
3. Each transaction creates a new `V2_FROM_PREFIX`/`V2_TO_PREFIX` entry keyed by `(prefix, from, victim)`/`(prefix, victim, from)` via `delegateV2`. [3](#0-2) 
4. Repeating for large N (e.g., tens of thousands) grows the victim's `from`-prefixed key set unbounded, since there is no cap in the store or actuator.
5. Any client then calls `GetDelegatedResourceAccountIndexV2` (or HTTP `/wallet/getdelegatedresourceaccountindexv2`) with the victim's address; `getWithPrefix` performs an unbounded `prefixQuery`, `ArrayList` construction, and sort over all N entries every time. [1](#0-0)

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

**File:** chainbase/src/main/java/org/tron/core/store/DelegatedResourceAccountIndexStore.java (L118-138)
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
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/DelegateResourceActuator.java (L147-150)
```java
    long delegateBalance = delegateResourceContract.getBalance();
    if (delegateBalance < TRX_PRECISION) {
      throw new ContractValidateException("delegateBalance must be greater than or equal to 1 TRX");
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

**File:** framework/src/main/java/org/tron/core/services/RpcApiService.java (L540-550)
```java
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
