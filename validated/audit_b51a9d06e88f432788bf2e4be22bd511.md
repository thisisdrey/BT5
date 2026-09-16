### Title
Unbounded growth of `DelegatedResourceAccountIndex` list enables gas/CPU-exhaustion DoS when queried via `GetDelegatedResourceAccountIndex` API - ([File: chainbase/src/main/java/org/tron/core/capsule/DelegatedResourceAccountIndexCapsule.java])

### Summary
When the `AllowDelegateOptimization` chain parameter has not been enabled, `FreezeBalanceActuator.delegateResource()` maintains a single, unbounded `DelegatedResourceAccountIndexCapsule` per account that accumulates every distinct address that account has ever delegated resource to/from. There is no cap on the number of entries, and the entire list is later read and serialized in full by the read-only `GetDelegatedResourceAccountIndex`/`GetDelegatedResourceAccountIndexV2` API, which is directly reachable by any anonymous gRPC/HTTP client. This mirrors the Teller `collateralAddresses` bug class: an unbounded array grown by ordinary transactions eventually makes a downstream operation (here, an API response) unboundedly expensive.

### Finding Description
In the legacy (non-optimized) delegation path, `delegateResource()` appends new addresses to the `toAccountsList`/`fromAccountsList` fields of a `DelegatedResourceAccountIndexCapsule`, guarding only against exact duplicates, never against unbounded size: [1](#0-0) 

The capsule itself provides no size limit when appending: [2](#0-1) 

An unprivileged account holder can trivially grow this list without bound by calling `FreezeBalanceContract` with `receiver_address` set to a large number of distinct addresses (each call requires only a small resource freeze amount, e.g. 1 sun, and a fresh, cheaply-generated receiver address), each addition costing normal transaction fees but never being rejected for size.

This same capsule (looked up directly by address key, `get(ownerAddress)`) is what is returned in full by the public, unauthenticated query API: [3](#0-2) 

reachable via gRPC: [4](#0-3) 

and via HTTP: [5](#0-4) 

Because the entire `toAccounts`/`fromAccounts` protobuf list is deserialized, copied, and serialized on every query with no pagination or limit, an attacker who has grown their own index to a very large size can cause each subsequent query (which any anonymous client can issue) to consume disproportionate CPU/memory on the node, degrading or crashing the query-serving path for all clients hitting that RPC/HTTP service.

### Impact Explanation
This is a resource-exhaustion / denial-of-service issue against a node's public query API surface, not a fund-theft bug. An attacker (using only cheap, self-controlled transactions) can force any node serving `GetDelegatedResourceAccountIndex`/`GetDelegatedResourceAccountIndexV2` requests for the attacker's address into excessive CPU/memory usage, degrading or blocking that query API for legitimate callers — matching the "API the node can no longer serve" impact category.

### Likelihood Explanation
Likelihood depends on whether `AllowDelegateOptimization` has been enabled on the target network via committee proposal; if it has been enabled, `delegateResource` uses the newer per-pair keyed index (`DelegatedResourceAccountIndexStore.delegateV2`/`unDelegateV2`) and each relation is a single fixed-size record rather than one unboundedly-growing list, avoiding this issue. On any deployment or network (e.g. private/enterprise chains, older testnets) where this proposal is not enabled, the unbounded legacy path remains fully reachable by an ordinary signed `FreezeBalanceContract` transaction from any account.

### Recommendation
- Enforce a maximum number of distinct `toAccounts`/`fromAccounts` entries per `DelegatedResourceAccountIndexCapsule` in the legacy (non-V2) delegation path, rejecting further delegations to new addresses once the cap is reached (mirroring the recommendation to cap `collateralAddresses` in the referenced report).
- Alternatively/complementarily, add pagination or a maximum returned-size limit to `getDelegatedResourceAccountIndex`/`getDelegatedResourceAccountIndexV2` so a single unbounded stored list cannot translate into an unbounded response payload/serialization cost.

### Proof of Concept
1. Ensure `AllowDelegateOptimization` is not enabled (default/legacy behavior).
2. From a single account, submit many `FreezeBalanceContract` transactions with `resource=BANDWIDTH` (or ENERGY), `delegate_resource=true`, and a unique `receiver_address` each time (each transaction only needs to freeze a minimal balance).
3. Each transaction appends a new entry to the caller's `DelegatedResourceAccountIndexCapsule.toAccountsList` via `FreezeBalanceActuator.delegateResource` ( [6](#0-5) ), which has no upper bound.
4. After accumulating a very large number of entries, issue repeated `GetDelegatedResourceAccountIndex` HTTP/gRPC queries for that address; each request forces the node to deserialize/serialize the full oversized list ( [3](#0-2) ), consuming disproportionate CPU/memory and degrading the API for other callers.

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

**File:** chainbase/src/main/java/org/tron/core/capsule/DelegatedResourceAccountIndexCapsule.java (L46-61)
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

**File:** framework/src/main/java/org/tron/core/services/http/GetDelegatedResourceAccountIndexServlet.java (L58-67)
```java
  private void fillResponse(ByteString address, boolean visible, HttpServletResponse response)
      throws IOException {
    DelegatedResourceAccountIndex reply =
        wallet.getDelegatedResourceAccountIndex(address);
    if (reply != null) {
      response.getWriter().println(JsonFormat.printToString(reply, visible));
    } else {
      response.getWriter().println("{}");
    }
  }
```
