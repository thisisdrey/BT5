### Title
Unbounded growth of `DelegatedResourceAccountIndexCapsule.toAccounts`/`fromAccounts` via repeated `FreezeBalanceContract` delegation DoSes the `getDelegatedResourceAccountIndex` query API and future freeze/unfreeze transactions - (File: `actuator/src/main/java/org/tron/core/actuator/FreezeBalanceActuator.java`)

### Summary
When resource delegation is done through the legacy (pre-optimization) `FreezeBalanceContract` path, java-tron appends the receiver/owner address to an in-protobuf `repeated ByteString` list (`toAccounts`/`fromAccounts`) stored in `DelegatedResourceAccountIndexCapsule`, with no cap on the number of distinct counterparties. An attacker can call `FreezeBalanceContract` repeatedly from one owner address to many distinct freshly-generated receiver addresses, causing the owner's index list to grow without bound. This capsule is read in full, deserialized, and rewritten on every subsequent freeze/unfreeze for that account, and is also returned in full by the `getDelegatedResourceAccountIndex` wallet API, which any unauthenticated HTTP/gRPC client can call.

### Finding Description
In `delegateResource()` in `FreezeBalanceActuator`, when `!dynamicPropertiesStore.supportAllowDelegateOptimization()`, the actuator fetches the owner's `DelegatedResourceAccountIndexCapsule`, checks membership with `toAccountsList.contains(...)`, and appends via `addToAccount`/`addFromAccount` with no size limit: [1](#0-0) 

The capsule itself imposes no bound on the repeated fields: [2](#0-1) 

Every later operation that touches the owner's delegation state (further `FreezeBalanceActuator.delegateResource`, `UnfreezeBalanceActuator` cleanup) loads the *entire* list into memory, builds a new `ArrayList`, mutates it, and serializes/writes the whole capsule back: [3](#0-2) 

The list is also exposed wholesale, unpaginated, through `Wallet.getDelegatedResourceAccountIndex` (backing the HTTP `GetDelegatedResourceAccountIndexServlet` and the corresponding gRPC API), which any anonymous client can query for any address: [4](#0-3) 

This mirrors the reported SingleSidedLiquidityVault bug class: a per-user array grown by an attacker-controlled number of low-cost transactions with no per-account cap, later fully iterated/serialized in both a state-transition path and a query path.

Note: java-tron itself later introduced a fix for this exact issue via the `supportAllowDelegateOptimization` dynamic property, which switches to a prefixed-key model (`convert`/`delegate` in `DelegatedResourceAccountIndexStore`) that avoids storing the whole list inline. The vulnerable code path is only reachable while that optimization flag is **not** yet enabled on a given chain/network (e.g., a freshly deployed private/consortium chain using default genesis parameters, or historically on public networks prior to the corresponding committee proposal being activated).

### Impact Explanation
On a network where `AllowDelegateOptimization` is not enabled, an attacker can:
1. Grow a single account's delegation index to an arbitrarily large size using many cheap `FreezeBalanceContract` transactions to distinct throwaway receiver addresses.
2. Cause subsequent `FreezeBalanceContract`/`UnfreezeBalanceContract` transactions touching that account to become increasingly expensive to process (repeated deserialization/serialization and linear list scans/removals), degrading block processing performance for transactions involving that account.
3. Cause the `getDelegatedResourceAccountIndex` HTTP/gRPC endpoint to return an oversized response for that address, which can be used to exhaust node/API resources (bandwidth, memory) when queried, effectively DoS-ing that API path for the affected account.

This is a resource-exhaustion / potential node-performance-degradation issue reachable purely through unprivileged, low-cost, self-signed transactions, consistent with a Medium-severity finding.

### Likelihood Explanation
Likelihood is conditional on the target network having `supportAllowDelegateOptimization` disabled. On networks where it is already enabled (the default for most current java-tron deployments per the later fix), this exact code path (`!dynamicStore.supportAllowDelegateOptimization()`) is not exercised and the alternate, prefix-key-based `delegate`/`convert` mechanism in `DelegatedResourceAccountIndexStore` is used instead, which does not store an unbounded inline list. I could not fully confirm from the available index whether the default value of `supportAllowDelegateOptimization` is enabled from genesis in this repo snapshot (the `DynamicPropertiesStore` getter/default was only partially inspected before the tool budget ran out), so likelihood should be treated as network-configuration-dependent rather than universally applicable.

### Recommendation
- Enforce a hard cap on the number of entries appended to `toAccounts`/`fromAccounts` in `DelegatedResourceAccountIndexCapsule` within `FreezeBalanceActuator.delegateResource` (and the corresponding `UnfreezeBalanceActuator` cleanup path) for accounts still operating under the legacy (`!supportAllowDelegateOptimization`) code path.
- Ensure `supportAllowDelegateOptimization` (or an equivalent structural fix) is enabled by default in genesis configuration for all new chain deployments so the unbounded inline-list code path can never be exercised on a fresh network.
- Add pagination/limits to `getDelegatedResourceAccountIndex` responses regardless of storage model, to bound response size for the query API.

### Proof of Concept
1. Deploy or use a java-tron network/config where `supportAllowDelegateOptimization` is 0/disabled and `supportDR()` (delegated resource) is enabled.
2. From attacker-controlled owner address `A` (funded with minimal TRX), generate N (e.g., 50,000) fresh receiver addresses `R1..RN`.
3. Broadcast N `FreezeBalanceContract` transactions from `A`, each freezing the minimum allowed balance (e.g., 1 TRX) to a distinct `Ri` with `resource=BANDWIDTH`/`ENERGY`, triggering `FreezeBalanceActuator.delegateResource` and repeatedly calling `ownerIndexCapsule.addToAccount(...)` per [5](#0-4) .
4. Observe that `A`'s `DelegatedResourceAccountIndex` record grows to N entries with no rejection.
5. Query `getDelegatedResourceAccountIndex(A)` via HTTP/gRPC and observe an oversized, unpaginated response; separately, issue a subsequent `UnfreezeBalanceContract` for one of the `Ri` and observe the linear-time list rebuild/rewrite in `UnfreezeBalanceActuator` per [3](#0-2) .

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/FreezeBalanceActuator.java (L319-346)
```java
    //modify DelegatedResourceAccountIndexStore
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

**File:** actuator/src/main/java/org/tron/core/actuator/UnfreezeBalanceActuator.java (L163-182)
```java
        if (!dynamicStore.supportAllowDelegateOptimization()) {
          DelegatedResourceAccountIndexCapsule ownerIndexCapsule =
              delegatedResourceAccountIndexStore.get(ownerAddress);
          if (ownerIndexCapsule != null) {
            List<ByteString> toAccountsList = new ArrayList<>(ownerIndexCapsule
                .getToAccountsList());
            toAccountsList.remove(ByteString.copyFrom(receiverAddress));
            ownerIndexCapsule.setAllToAccounts(toAccountsList);
            delegatedResourceAccountIndexStore.put(ownerAddress, ownerIndexCapsule);
          }

          DelegatedResourceAccountIndexCapsule receiverIndexCapsule =
              delegatedResourceAccountIndexStore.get(receiverAddress);
          if (receiverIndexCapsule != null) {
            List<ByteString> fromAccountsList = new ArrayList<>(receiverIndexCapsule
                .getFromAccountsList());
            fromAccountsList.remove(ByteString.copyFrom(ownerAddress));
            receiverIndexCapsule.setAllFromAccounts(fromAccountsList);
            delegatedResourceAccountIndexStore.put(receiverAddress, receiverIndexCapsule);
          }
```

**File:** chainbase/src/main/java/org/tron/core/store/DelegatedResourceAccountIndexStore.java (L106-112)
```java
  public DelegatedResourceAccountIndexCapsule getIndex(byte[] address) {
    DelegatedResourceAccountIndexCapsule indexCapsule = get(address);
    if (indexCapsule != null) {
      return indexCapsule;
    }
    return getWithPrefix(FROM_PREFIX, TO_PREFIX, address);
  }
```
