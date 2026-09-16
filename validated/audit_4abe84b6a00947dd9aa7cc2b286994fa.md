### Title
Unbounded growth of `DelegatedResourceAccountIndexCapsule` to/from-account lists enables quadratic-cost and unbounded-loop DoS - ([File: actuator/src/main/java/org/tron/core/actuator/FreezeBalanceActuator.java])

### Summary
The legacy resource-delegation bookkeeping path (`FreezeBalanceActuator`/`UnfreezeBalanceActuator`, active whenever `DynamicPropertiesStore.supportAllowDelegateOptimization()` is false) stores every distinct delegation counterpart of an address in a single protobuf-backed list (`toAccounts`/`fromAccounts`) inside `DelegatedResourceAccountIndexCapsule`, with no upper bound on list size, mirroring the reported `openPositions[]` unbounded-array pattern.

### Finding Description
In `FreezeBalanceActuator.delegateResource`, when the chain has not enabled `supportAllowDelegateOptimization`, every call to freeze-and-delegate resources to a new receiver appends that receiver address to the owner's `DelegatedResourceAccountIndexCapsule.toAccounts` list (and symmetrically the owner to the receiver's `fromAccounts` list), guarded only by a linear `List.contains()` check: [1](#0-0) 

Nothing limits how many distinct receiver (or sender) addresses an account can accumulate — a single account can be made the target/source of an arbitrarily large number of delegations, each broadcastable via a plain signed `FreezeBalanceContract` transaction. Each such call also performs an O(n) `contains()` scan of the growing list before append, so `n` sequential delegations from one owner to `n` distinct receivers cost O(n²) total work with no additional energy/fee charged (`calcFee()` for this actuator returns 0 for delegation bookkeeping) — the actuator does not scale its cost with list size.

The same underlying `DelegatedResourceAccountIndexCapsule` list is looped over unconditionally in multiple additional code paths reachable from ordinary transactions and public queries:
- `DelegatedResourceAccountIndexStore.convert(address)` iterates the full legacy `toAccounts`/`fromAccounts` lists to migrate them to the V2 indexing scheme; this is invoked from `DelegateResourceActuator`/`FreezeBalanceActuator` execution paths the first time an address participates in the new delegation mode, meaning a single transaction can trigger an unbounded loop over data an attacker pre-built: [2](#0-1) 
- `getWithPrefix` (used by `getIndex`/`getV2Index`) performs a `prefixQuery` and sorts the entire result set, with no page limit, and is reachable via the public `Wallet.getDelegatedResourceAccountIndex`/`getDelegatedResourceAccountIndexV2` API, itself exposed through gRPC (`RpcApiService`, `RpcApiServiceOnPBFT`, `RpcApiServiceOnSolidity`) and HTTP (`GetDelegatedResourceAccountIndexServlet`) endpoints callable by any anonymous client: [3](#0-2) [4](#0-3) 
- `UnfreezeBalanceActuator` also mutates these unbounded lists via `ArrayList` copy/remove operations proportional to list size on every unfreeze: [5](#0-4) 

Because list growth is driven purely by the number of distinct delegation counterparties an attacker chooses to use (freezing a tiny, dust-level amount to each newly generated receiver address is sufficient — the code performs no minimum-amount enforcement in this bookkeeping path and no cap on list length), an attacker with modest capital can inflate the list to an arbitrary size using many low-cost transactions.

### Impact Explanation
- Any single subsequent transaction that hits `convert()` for a poisoned address performs an unbounded, un-metered loop of `delegate()` calls (one `put()` to a `TronStoreWithRevoking` per legacy entry), which can make block-application for that transaction disproportionately expensive relative to the energy actually charged, since `calcFee()` in `FreezeBalanceActuator`/`DelegateResourceActuator` returns 0 for this bookkeeping and does not scale with `toAccounts`/`fromAccounts` size.
- The public query API `getDelegatedResourceAccountIndex`/`getDelegatedResourceAccountIndexV2`, reachable via unauthenticated gRPC/HTTP calls, performs an unbounded `prefixQuery` + full in-memory sort over the attacker-inflated dataset, allowing a remote, unprivileged client to force expensive node-side work on every query against the poisoned address, which is a node resource-exhaustion / API-availability risk (an anonymous API client can no longer be served efficiently) rather than a fund-theft issue.
- This does not by itself cause block-gap consensus failure or fund loss; the primary confirmed impact is CPU/DB-scan cost inflation on both the actuator execution and the read-only query path, consistent with the "Medium" severity DoS class of the original report.

### Likelihood Explanation
Reaching this requires only ordinary, permission-less transactions: freezing/delegating TRX to many distinct newly generated receiver addresses is a normal, low-cost operation available to any account holder, with no special privilege or contract needed. The attack is bounded only by the attacker's willingness to pay ordinary transaction fees for each delegation, making the likelihood moderate-to-high for a determined actor targeting the query API or a future `convert()` trigger.

### Recommendation
- Cap the number of entries in `DelegatedResourceAccountIndexCapsule.toAccounts`/`fromAccounts` (and enforce it in `FreezeBalanceActuator`/`DelegateResourceActuator`/`UnfreezeBalanceActuator`), or migrate fully away from the embedded-list model to a per-pair keyed store (which the V2 `delegate()`/`unDelegate()` scheme already does) and remove/disable the legacy unbounded-list path entirely.
- Add pagination/limit parameters to `DelegatedResourceAccountIndexStore.getWithPrefix` and the corresponding `Wallet`/gRPC/HTTP query methods so a single unauthenticated request cannot force an unbounded prefix scan and sort.
- Charge actuator fees proportional to the size of `convert()`/list-mutation work performed, or amortize/limit the number of legacy entries converted per transaction.

### Proof of Concept
1. Ensure `supportAllowDelegateOptimization` is disabled (or, for the `convert()` path, later enabled after list has grown) on the target chain.
2. From a single funded account `A`, submit `N` (e.g., tens of thousands of) `FreezeBalanceContract` transactions, each delegating a minimal frozen balance to a freshly generated, distinct receiver address, driving growth of `A`'s `DelegatedResourceAccountIndexCapsule.toAccounts` list via `FreezeBalanceActuator.delegateResource` ( [1](#0-0) ).
3. Call the public `getDelegatedResourceAccountIndex` API (gRPC/HTTP) for address `A` and observe the cost of the unbounded `prefixQuery` + sort in `getWithPrefix` ( [3](#0-2) ) scaling with `N`.
4. Alternatively, once `supportAllowDelegateOptimization` becomes enabled on-chain, submit a single new `DelegateResourceContract`/`FreezeBalanceContract` transaction from address `A`; observe `convert(A)` performing `N` sequential `delegate()`/store `put()` calls within that one transaction's execution ( [2](#0-1) ), disproportionate to the fee charged for that transaction.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/FreezeBalanceActuator.java (L319-345)
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

**File:** chainbase/src/main/java/org/tron/core/store/DelegatedResourceAccountIndexStore.java (L42-61)
```java
  public void convert(byte[] address) {
    DelegatedResourceAccountIndexCapsule indexCapsule = this.get(address);
    if (indexCapsule == null) {
      // convert complete or have no delegate
      return;
    }
    // convert old data
    List<ByteString> toList = indexCapsule.getToAccountsList();
    for (int i = 0; i < toList.size(); i++) {
      // use index as the timestamp, just to keep index in order
      this.delegate(address, toList.get(i).toByteArray(), i + 1L);
    }

    List<ByteString> fromList = indexCapsule.getFromAccountsList();
    for (int i = 0; i < fromList.size(); i++) {
      // use index as the timestamp, just to keep index in order
      this.delegate(fromList.get(i).toByteArray(), address, i + 1L);
    }
    this.delete(address);
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
