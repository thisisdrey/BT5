### Title
Unbounded, low-cost growth of `DelegatedResourceAccountIndex` allows griefing/DoS of the delegated-resource index query API - (File: `chainbase/src/main/java/org/tron/core/store/DelegatedResourceAccountIndexStore.java`)

### Summary
The Solidity report shows that `VotingEscrow.delegate()` lets any account append itself to another address's delegate array without a strict enough cap (`MAX_DELEGATES = 1024`), so an attacker can cheaply inflate a victim's delegate list and make routine operations on that victim disproportionately expensive. java-tron has a structurally analogous pattern in the TRX resource-delegation subsystem: any unprivileged account can, at near-zero cost, register itself as a delegator/delegatee against an arbitrary victim address by calling `DelegateResourceContract`, and there is no upper bound on how many such index entries a single address can accumulate.

### Finding Description
`DelegateResourceActuator.delegateResource()` unconditionally calls `DelegatedResourceAccountIndexStore.delegateV2(ownerAddress, receiverAddress, timestamp)` for every delegate operation: [1](#0-0) 

`delegateV2` simply writes two new keyed entries — `V2_FROM_PREFIX+from+to` and `V2_TO_PREFIX+to+from` — with no limit on how many distinct `from`/`to` pairs may exist for a given address: [2](#0-1) 

The only economic barrier to creating a delegation entry is `delegateBalance >= TRX_PRECISION` (i.e. ≥ 1 TRX) enforced in `DelegateResourceActuator.validate()`: [3](#0-2) 

There is no analog to `VotingEscrow`'s `MAX_DELEGATES` check anywhere in `DelegateResourceActuator`, `DelegateResourceProcessor` (the TVM native-contract path), or `DelegatedResourceAccountIndexStore`. An attacker can therefore create an unbounded number of throwaway accounts, freeze 1 TRX each, and issue `DelegateResourceContract` transactions naming an arbitrary victim address as `receiverAddress`. Each such transaction is cheap (fixed bandwidth/energy cost, independent of the victim's existing index size) and does not require the victim's cooperation, since only the receiver address (not signature) is referenced.

The resulting inflated index is read back through `getIndex`/`getV2Index`, which perform an unbounded RocksDB prefix scan over all entries for the queried address, materialize every entry into a `DelegatedResourceAccountIndexCapsule`, and sort the whole collection by timestamp: [4](#0-3) 

This query is exposed directly to any unauthenticated caller through the gRPC/HTTP/Wallet query surface: [5](#0-4) 

and the equivalent HTTP servlets (`GetDelegatedResourceAccountIndexServlet`, `GetDelegatedResourceAccountIndexV2Servlet`, plus the PBFT/Solidity node variants) reachable via `Wallet.getDelegatedResourceAccountIndex` / `getDelegatedResourceAccountIndexV2`.

Additionally, the legacy (pre-`AllowDelegateOptimization`) index representation stores the *entire* `toAccountsList`/`fromAccountsList` as a single Protobuf blob per address, which is fully deserialized, copied into an `ArrayList`, mutated, and re-serialized on every add/remove — an O(n) cost per state-changing call as the list grows, mirroring the exact "single unbounded array field mutated on every state-changing call" pattern from the Solidity report: [6](#0-5) [7](#0-6) 

### Impact Explanation
The V2 delegation-index model does not force any single account's own state-changing transaction to iterate the inflated list (writes are O(1) key puts/deletes), so this does not directly translate into "100x more expensive transfer" the way the Solidity bug does for a fee-metered EVM transfer. The concretely demonstrable impact is instead against the **read query API**: an attacker can cheaply grow the number of `DelegatedResourceAccountIndex` entries associated with any victim address to an arbitrary size, causing `getDelegatedResourceAccountIndex`/`getDelegatedResourceAccountIndexV2` (gRPC, HTTP, PBFT, and Solidity-node variants) to perform increasingly expensive prefix scans, deserialization, and sorting for that specific address — degrading or effectively denying that API for the targeted address without requiring any privileged access. This falls into the "API the node can no longer serve" category permitted by the validation rules, though it is a narrower, per-address service-degradation issue rather than a chain-halting DoS.

### Likelihood Explanation
Likelihood is high for triggering the condition: the attack requires only cheap, repeatable, unprivileged transactions (`FreezeBalanceV2`/freeze + `DelegateResourceContract` with 1 TRX balance) from freely creatable accounts, naming any address as receiver with no consent check. It does not require special permissions, a malicious SR/witness, or network-level access — it is reachable from a single, ordinary broadcast transaction stream.

### Recommendation
- Introduce an explicit maximum number of distinct delegators/delegatees tracked per address in `DelegatedResourceAccountIndexStore` (analogous to `MAX_DELEGATES`), enforced in `DelegateResourceActuator.validate()`/`DelegateResourceProcessor.validate()` before allowing a new `delegateV2` entry to be created for a receiver that has not consented.
- For the read path, bound/paginate `getWithPrefix()`'s prefix scan (e.g., a maximum entries-returned cap or required pagination cursor) so a single query cannot be forced to process unbounded data regardless of how the index was populated.
- Consider requiring a minimum economic cost proportional to the number of existing entries for a given receiver (progressive cost), similar in spirit to the report's suggested reduction of `MAX_DELEGATES` to balance functionality against cost of abuse.

### Proof of Concept
Conceptual (I could not execute this against a live node, but it follows directly from the cited code paths):
1. Generate N throwaway accounts; fund each with the minimum TRX needed to freeze ≥1 TRX for bandwidth/energy (`FreezeBalanceV2Contract`).
2. From each throwaway account, submit a `DelegateResourceContract` with `receiver_address` = victim's address and `balance` = 1 TRX (`TRX_PRECISION`), satisfying `DelegateResourceActuator.validate()`'s only balance check.
3. Each transaction calls `delegateResource()` → `DelegatedResourceAccountIndexStore.delegateV2()`, adding one `V2_TO_PREFIX+victim+from` entry with no cap enforced.
4. Repeat for large N (limited only by attacker's TRX budget for 1-TRX freezes, which can be reused/unfrozen and re-delegated).
5. Query `getDelegatedResourceAccountIndexV2(victim_address)` via gRPC/HTTP; observe `getWithPrefix()` performing a prefix scan and sort over N entries, with response latency/CPU cost scaling with N — reproducing the "query cost grows unbounded with attacker-controlled input" pattern described in the Solidity report.

Note: I was not able to run this against a live java-tron node to measure exact CPU/latency scaling (no execution environment available here); the analysis is based on static code review of the cited actuator, store, and API entry points confirming the absence of any size cap comparable to `MAX_DELEGATES`.

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

**File:** actuator/src/main/java/org/tron/core/actuator/UnfreezeBalanceActuator.java (L162-182)
```java
        //modify DelegatedResourceAccountIndexStore
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
