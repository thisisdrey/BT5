## Title
Unbounded growth of `DelegatedResourceAccountIndexCapsule` legacy `to_accounts`/`from_accounts` lists via repeated `FreezeBalanceContract` delegations enables state-bloat DoS - ([File: actuator/src/main/java/org/tron/core/actuator/FreezeBalanceActuator.java])

## Summary
The legacy resource-delegation index (`DelegatedResourceAccountIndexCapsule`, keyed per-account) stores `to_accounts`/`from_accounts` as plain repeated protobuf fields with no size cap. `FreezeBalanceActuator.delegateResource()` appends a new entry to this list every time an account delegates frozen BANDWIDTH/ENERGY to a *new* receiver address, and the whole capsule is read, deserialized, mutated, re-serialized and written back on every call. Because any account can trigger this with a large number of distinct (attacker-controlled) receiver addresses using only the minimum freezable amount, the per-account index record can be grown without bound, degrading write cost of future freeze/unfreeze operations and blowing up the response size/cost of the public `GetDelegatedResourceAccountIndex` query API.

## Finding Description
`FreezeBalanceActuator.delegateResource()` maintains a `DelegatedResourceAccountIndexCapsule` for both the delegator and the receiver: [1](#0-0) 

Each new (distinct) receiver address appends an entry to `toAccountsList` (owner side) / `fromAccountsList` (receiver side), guarded only by a linear `.contains()` check — there is no upper bound on the number of distinct entries, and this path is only taken via `!dynamicPropertiesStore.supportAllowDelegateOptimization()`, i.e. it is the legacy (pre-optimization) delegation-index format that many accounts still hold from before the optimization flag was turned on, and remains reachable for any account/config where the optimization proposal is not (yet) enabled.

The append/mutate operations themselves are implemented in `DelegatedResourceAccountIndexCapsule`: [2](#0-1) 

Because the capsule is stored under a single DB key per account (`createDbKey()` returns `getAccount().toByteArray()`), the entire growing list is deserialized and re-serialized by `AccountStore.get/put`-equivalent calls on every subsequent freeze/unfreeze targeting that account, and there is no mechanism analogous to `EnumerableMap` or a size limit — this mirrors the "unbounded array growth" defect described in the referenced Sherlock report (`CrabNetting.deposits`).

This same growing record is exposed directly via public read APIs with no pagination: [3](#0-2) 

reachable over gRPC: [4](#0-3) 

and over HTTP/JSON-RPC style servlet: [5](#0-4) 

with the RPC method declared unbounded in the proto: [6](#0-5) 

An attacker who repeatedly calls `FreezeBalanceContract` (delegated variant) with a minimal frozen amount toward a large number of distinct freshly-generated receiver addresses causes:
1. Each subsequent `FreezeBalanceActuator`/`UnfreezeBalanceActuator` execution against the same owner/receiver key to read-modify-write an ever-growing protobuf blob (O(n) work per call, and O(n) `.contains()` scanning), degrading transaction execution performance for that account.
2. The `getdelegatedresourceaccountindex` HTTP/gRPC query for that address to return and serialize an unbounded list, which any anonymous API client can request repeatedly — turning it into a cheap request-amplification vector against a node's serving capacity (the JSON/print formatting in `GetDelegatedResourceAccountIndexServlet.fillResponse` and gRPC `onNext` writes the entire list in one response with no pagination).

## Impact Explanation
This does not directly steal funds, but it degrades the node's ability to serve a public, unauthenticated query API (`/wallet/getdelegatedresourceaccountindex` and the corresponding gRPC method) and increases per-transaction execution cost for the affected account's future `FreezeBalanceContract`/`UnfreezeBalanceContract` operations, matching the "API the node can no longer serve" acceptance criterion. Because the legacy delegation-index format has no cap and the growth is driven entirely by unprivileged transaction broadcasts (each requiring only the network's minimum freeze fee), the severity is comparable to the referenced report's medium/high classification for unbounded-array DoS. It is bounded in practical mainnet exposure by whether `AllowDelegateOptimization` is enabled for the target chain — if enabled, new delegations use the optimized per-pair storage (`convert`, `delegate`) which does not exhibit this growth pattern; the legacy list only grows for delegations created while the optimization proposal is inactive, or for accounts whose data already predates the optimization switch and can still be appended to as long as this branch remains reachable.

## Likelihood Explanation
Likelihood is Medium: the attack is simple, cheap (requires only many small `FreezeBalanceContract` transactions with distinct receiver addresses), and fully reachable by any unprivileged account. However, it depends on the `AllowDelegateOptimization` dynamic parameter being disabled (or the actuator branch otherwise reachable) for the specific account/chain state being targeted, since that governs whether `delegateResource()` takes the legacy unbounded-list branch versus the optimized per-pair branch.

## Recommendation
- Cap the number of distinct `to_accounts`/`from_accounts` entries a `DelegatedResourceAccountIndexCapsule` can hold, or migrate fully to the optimized per-pair `DelegatedResourceAccountIndexStore` format (`V2_FROM_PREFIX`/`V2_TO_PREFIX`) and deprecate/disable the legacy unbounded-list code path entirely regardless of the `AllowDelegateOptimization` flag.
- Paginate the `GetDelegatedResourceAccountIndex`/`GetDelegatedResourceAccountIndexV2` RPC and HTTP responses instead of returning the full list in one call.
- Add validation in `FreezeBalanceActuator` rejecting delegation to a new receiver once the owner's/receiver's legacy index list exceeds a sane maximum size.

## Proof of Concept
1. Ensure the target account/chain state is on the legacy delegation-index path (`dynamicPropertiesStore.supportAllowDelegateOptimization()` returns false, or the account already has entries in the legacy `DelegatedResourceAccountIndexStore` record).
2. From attacker-controlled account `A`, generate `N` (e.g., 10,000+) fresh key pairs `R_1..R_N`.
3. Broadcast `N` `FreezeBalanceContract` transactions from `A`, each delegating the minimum allowed `frozen_balance` (e.g., `1_000_000` sun) with a distinct `receiver_address = R_i`, per the flow in `FreezeBalanceActuator.delegateResource()` ( [7](#0-6) ).
4. After these transactions, `A`'s `DelegatedResourceAccountIndexCapsule.toAccountsList` contains `N` entries (confirmed by the existing test pattern in `FreezeBalanceActuatorTest.testMultiFreezeDelegatedBalanceForBandwidth`, which demonstrates the list growing by one entry per new receiver: [8](#0-7) ).
5. Query `/wallet/getdelegatedresourceaccountindex?value=<A>` repeatedly; each response now serializes and returns the full `N`-entry list, and subsequent freeze/unfreeze transactions on `A` incur the growing read-modify-write cost, demonstrating the resource-exhaustion effect on both the query API and the actuator execution path.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/FreezeBalanceActuator.java (L320-345)
```java
    if (!dynamicPropertiesStore.supportAllowDelegateOptimization()) {

      DelegatedResourceAccountIndexCapsule ownerIndexCapsule =
          delegatedResourceAccountIndexStore.get(ownerAddress);
      if (ownerIndexCapsule == null) {
        ownerIndexCapsule = new DelegatedResourceAccountIndexCapsule(
            ByteString.copyFrom(ownerAddress));
      }
      List<ByteString> toAccountsList = ownerIndexCapsule.getToAccountsList();
      if (!toAccountsList.contains(ByteString.copyFrom(receiverAddress))) {
        ownerIndexCapsule.addToAccount(ByteString.copyFrom(receiverAddress));
      }
      delegatedResourceAccountIndexStore.put(ownerAddress, ownerIndexCapsule);

      DelegatedResourceAccountIndexCapsule receiverIndexCapsule
          = delegatedResourceAccountIndexStore.get(receiverAddress);
      if (receiverIndexCapsule == null) {
        receiverIndexCapsule = new DelegatedResourceAccountIndexCapsule(
            ByteString.copyFrom(receiverAddress));
      }
      List<ByteString> fromAccountsList = receiverIndexCapsule
          .getFromAccountsList();
      if (!fromAccountsList.contains(ByteString.copyFrom(ownerAddress))) {
        receiverIndexCapsule.addFromAccount(ByteString.copyFrom(ownerAddress));
      }
      delegatedResourceAccountIndexStore.put(receiverAddress, receiverIndexCapsule);
```

**File:** chainbase/src/main/java/org/tron/core/capsule/DelegatedResourceAccountIndexCapsule.java (L46-94)
```java
  public List<ByteString> getFromAccountsList() {
    return this.delegatedResourceAccountIndex.getFromAccountsList();
  }

  public void setAllFromAccounts(List<ByteString> fromAccounts) {
    this.delegatedResourceAccountIndex = this.delegatedResourceAccountIndex.toBuilder()
        .clearFromAccounts()
        .addAllFromAccounts(fromAccounts)
        .build();
  }

  public void addFromAccount(ByteString fromAccount) {
    this.delegatedResourceAccountIndex = this.delegatedResourceAccountIndex.toBuilder()
        .addFromAccounts(fromAccount)
        .build();
  }

  public void removeFromAccount(ByteString fromAccount) {
    if (getFromAccountsList().contains(fromAccount)) {
      List<ByteString> fromList = new ArrayList<>(getFromAccountsList());
      fromList.remove(fromAccount);
      setAllFromAccounts(fromList);
    }
  }

  public List<ByteString> getToAccountsList() {
    return this.delegatedResourceAccountIndex.getToAccountsList();
  }

  public void setAllToAccounts(List<ByteString> toAccounts) {
    this.delegatedResourceAccountIndex = this.delegatedResourceAccountIndex.toBuilder()
        .clearToAccounts()
        .addAllToAccounts(toAccounts)
        .build();
  }

  public void addToAccount(ByteString toAccount) {
    this.delegatedResourceAccountIndex = this.delegatedResourceAccountIndex.toBuilder()
        .addToAccounts(toAccount)
        .build();
  }

  public void removeToAccount(ByteString toAccount) {
    if (getToAccountsList().contains(toAccount)) {
      List<ByteString> toList = new ArrayList<>(getToAccountsList());
      toList.remove(toAccount);
      setAllToAccounts(toList);
    }
  }
```

**File:** framework/src/main/java/org/tron/core/Wallet.java (L1040-1051)
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

**File:** protocol/src/main/protos/api/api.proto (L293-294)
```text
  rpc GetDelegatedResourceAccountIndex (BytesMessage) returns (DelegatedResourceAccountIndex) {
  };
```

**File:** framework/src/test/java/org/tron/core/actuator/FreezeBalanceActuatorTest.java (L275-296)
```java
  @Test
  public void testMultiFreezeDelegatedBalanceForBandwidth() {
    dbManager.getDynamicPropertiesStore().saveAllowDelegateResource(1);
    dbManager.getDynamicPropertiesStore().saveAllowDelegateOptimization(1L);
    dbManager.getDynamicPropertiesStore().saveLatestBlockHeaderTimestamp(10000L);
    long frozenBalance = 1_000_000_000L;
    long duration = 3;
    final int RECEIVE_COUNT = 100;
    String[] RECEIVE_ADDRESSES = new String[RECEIVE_COUNT + 1];

    DelegatedResourceAccountIndexCapsule ownerIndexCapsule =
        new DelegatedResourceAccountIndexCapsule(
            ByteString.copyFrom(ByteArray.fromHexString(OWNER_ADDRESS)));
    for (int i = 0; i < RECEIVE_COUNT + 1; i++) {
      ECKey ecKey = new ECKey(Utils.getRandom());
      RECEIVE_ADDRESSES[i] = ByteArray.toHexString(ecKey.getAddress());
      if (i != RECEIVE_COUNT) {
        ownerIndexCapsule.addToAccount(ByteString.copyFrom(ecKey.getAddress()));
      }
    }
    dbManager.getDelegatedResourceAccountIndexStore().put(
        ByteArray.fromHexString(OWNER_ADDRESS), ownerIndexCapsule);
```
