Found the analog. This confirms an unbounded growth vulnerability directly analogous to the reported bug.

### Title
Unbounded growth of `DelegatedResourceAccountIndex` `fromAccounts`/`toAccounts` lists via unauthorized resource delegation causes API/query DoS - (File: actuator/src/main/java/org/tron/core/actuator/DelegateResourceActuator.java)

### Summary
`DelegateResourceActuator.delegateResource()` (V2 model) and `FreezeBalanceActuator.delegateResource()` (legacy model) allow any account owner to push an entry into an arbitrary `receiverAddress`'s `DelegatedResourceAccountIndexCapsule.fromAccounts` list without any consent, cap, or minimum-amount check from the receiver. This mirrors the Vesting.sol bug class: anyone can "push" into another account's per-account list, growing it unboundedly, and that list is later read/iterated in full by query paths and by `unDelegate`/`convert` logic.

### Finding Description
In the new resource model, `DelegateResourceProcessor.delegateResource()` (TVM native contract path) and `DelegateResourceActuator.delegateResource()` (transaction path) write two new keys per delegation call: `V2_FROM_PREFIX + owner + receiver` and `V2_TO_PREFIX + receiver + owner`. [1](#0-0) [2](#0-1) 

There is no validation that `receiverAddress != ownerAddress`'s consent is required, no minimum delegated balance is enforced beyond `> 0`, and no cap on the number of distinct delegators per receiver. Any account can call `DelegateResourceContract` with a trivial `balance` (as small as `1`) against a fixed victim `receiverAddress`, using a fresh, cheaply generated owner address each time (freezing 1 sun for bandwidth/energy is trivially cheap), causing an unbounded number of unique `fromAccounts` entries to accumulate for that victim.

When these entries are queried, `DelegatedResourceAccountIndexStore.getWithPrefix()` performs a full `prefixQuery` scan and materializes/sorts the entire list into memory: [3](#0-2) 

This is reachable via the public HTTP/gRPC query APIs `getDelegatedResourceAccountIndex(V2)`, exposed with no pagination and no result-size cap: [4](#0-3) [5](#0-4) 

Additionally, in the legacy (non-optimized) delegate model, `FreezeBalanceActuator.delegateResource()` mutates the victim's `DelegatedResourceAccountIndexCapsule` in-place via `List.contains()` checks over the growing `fromAccountsList`/`toAccountsList` (O(n) per call, O(n²) total growth cost), and the whole capsule (a single protobuf value) must be deserialized, scanned, and reserialized on every subsequent delegate/undelegate/freeze/unfreeze touching that address: [6](#0-5) [7](#0-6) 

Test code even exercises this pattern with 100 receiver entries added directly to demonstrate the store growth, confirming the codebase is aware the index list can hold an unbounded number of entries without any pruning: [8](#0-7) 

### Impact Explanation
An attacker can permanently degrade or crash the node's ability to serve `getDelegatedResourceAccountIndex`/`getDelegatedResourceAccountIndexV2` queries (HTTP, gRPC, and the underlying `Wallet` API) for any targeted victim address by repeatedly issuing trivial-value `DelegateResourceContract` transactions from many throwaway owner addresses, each adding one entry to the victim's `fromAccounts` list. Because the store performs a full prefix scan and in-memory sort on every read (`getWithPrefix`), and because the legacy path also mutates a single ever-growing protobuf blob on every write, this can consume excessive CPU/memory for both the query-serving path and the block-processing/actuator-execution path, degrading node responsiveness. This does not directly cause fund loss but is a concrete resource-exhaustion/API-denial issue reachable by any unprivileged transaction broadcaster or query client, without requiring cooperation from the victim.

### Likelihood Explanation
High feasibility: freezing/delegating requires only owning a small amount of TRX (as little as 1 sun of bandwidth/energy) per throwaway account, and there is no check preventing many distinct owner addresses from delegating tiny amounts to the same receiver. No special privileges or victim cooperation are required — this exactly matches the reachable, unprivileged transaction-broadcaster threat model.

### Recommendation
- Enforce a minimum delegated balance per `DelegateResourceContract`/`FreezeBalanceContract` (with receiver) transaction, similar to the report's suggested fix of a minimum vestment amount, to make list-inflation economically infeasible.
- Cap the maximum number of distinct `fromAccounts`/`toAccounts` entries tracked per address, or migrate the index fully to a paginated/prefix-scan-with-limit read pattern instead of returning the full list.
- Add pagination parameters to `getDelegatedResourceAccountIndex`/`V2` APIs so a single query cannot force an unbounded full-list materialization.
- Consider requiring receiver opt-in/acknowledgement, or at least deduplicating/limiting per-owner entries, rather than allowing free unauthorized list growth on the receiver's index.

### Proof of Concept
1. Attacker generates N throwaway keypairs (N large, e.g. tens of thousands), funds each with the minimal amount needed to freeze/delegate 1 sun of bandwidth or energy.
2. For each throwaway address, attacker broadcasts a `FreezeBalanceContract`/`DelegateResourceContract` with `receiverAddress` = victim's address and `frozenBalance`/`balance` = 1 (minimum allowed).
   - Legacy path: `FreezeBalanceActuator.delegateResource()` appends to `receiverIndexCapsule.fromAccountsList` after an O(n) `contains()` check [9](#0-8) .
   - V2 path: `DelegateResourceProcessor.delegateResource()`/`DelegateResourceActuator.delegateResource()` unconditionally writes a new `V2_TO_PREFIX+receiver+owner` key [2](#0-1) .
3. After accumulating a large number of entries, any client calls `GET /wallet/getdelegatedresourceaccountindex` (or `V2`) for the victim's address; `Wallet.getDelegatedResourceAccountIndex(V2)` triggers `DelegatedResourceAccountIndexStore.getWithPrefix()`, which performs a full prefix scan, materializes, and sorts the entire (attacker-inflated) list [3](#0-2) , consuming disproportionate CPU/memory and degrading/denying that query for the victim, while also imposing repeated deserialize/scan/reserialize costs on the legacy in-place-list codepath for every subsequent freeze/unfreeze/delegate transaction touching the victim address.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/DelegateResourceProcessor.java (L166-181)
```java
    //modify DelegatedResourceAccountIndex
    long now = repo.getDynamicPropertiesStore().getLatestBlockHeaderTimestamp();
    byte[] fromKey = Bytes.concat(
        DelegatedResourceAccountIndexStore.getV2_FROM_PREFIX(), ownerAddress, receiverAddress);
    DelegatedResourceAccountIndexCapsule toIndexCapsule =
        new DelegatedResourceAccountIndexCapsule(ByteString.copyFrom(receiverAddress));
    toIndexCapsule.setTimestamp(now);
    repo.updateDelegatedResourceAccountIndex(fromKey, toIndexCapsule);

    byte[] toKey = Bytes.concat(
        DelegatedResourceAccountIndexStore.getV2_TO_PREFIX(), receiverAddress, ownerAddress);
    DelegatedResourceAccountIndexCapsule fromIndexCapsule =
        new DelegatedResourceAccountIndexCapsule(ByteString.copyFrom(ownerAddress));
    fromIndexCapsule.setTimestamp(now);
    repo.updateDelegatedResourceAccountIndex(toKey, fromIndexCapsule);

```

**File:** actuator/src/main/java/org/tron/core/actuator/DelegateResourceActuator.java (L313-316)
```java
    //modify DelegatedResourceAccountIndexStore
    delegatedResourceAccountIndexStore.delegateV2(ownerAddress, receiverAddress,
        dynamicPropertiesStore.getLatestBlockHeaderTimestamp());

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

**File:** actuator/src/main/java/org/tron/core/actuator/FreezeBalanceActuator.java (L319-353)
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

    } else {
      // modify DelegatedResourceAccountIndexStore new
      delegatedResourceAccountIndexStore.convert(ownerAddress);
      delegatedResourceAccountIndexStore.convert(receiverAddress);
      delegatedResourceAccountIndexStore.delegate(ownerAddress, receiverAddress,
          dynamicPropertiesStore.getLatestBlockHeaderTimestamp());
    }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/DelegatedResourceAccountIndexCapsule.java (L63-69)
```java
  public void removeFromAccount(ByteString fromAccount) {
    if (getFromAccountsList().contains(fromAccount)) {
      List<ByteString> fromList = new ArrayList<>(getFromAccountsList());
      fromList.remove(fromAccount);
      setAllFromAccounts(fromList);
    }
  }
```

**File:** framework/src/test/java/org/tron/core/actuator/FreezeBalanceActuatorTest.java (L276-337)
```java
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
    AccountCapsule receiverCapsule =
        new AccountCapsule(
            ByteString.copyFromUtf8("receiver"),
            ByteString.copyFrom(ByteArray.fromHexString(RECEIVE_ADDRESSES[RECEIVE_COUNT])),
            AccountType.Normal,
            initBalance);
    dbManager.getAccountStore().put(receiverCapsule.getAddress().toByteArray(), receiverCapsule);

    TransactionResultCapsule ret = new TransactionResultCapsule();
    FreezeBalanceActuator actuator = new FreezeBalanceActuator();
    actuator.setChainBaseManager(dbManager.getChainBaseManager())
        .setAny(getDelegatedContractForBandwidth(
            OWNER_ADDRESS, RECEIVE_ADDRESSES[RECEIVE_COUNT], frozenBalance, duration));
    try {
      ownerIndexCapsule = dbManager
          .getDelegatedResourceAccountIndexStore().getIndex(ByteArray.fromHexString(OWNER_ADDRESS));
      List<ByteString> beforeList = ownerIndexCapsule.getToAccountsList();
      actuator.validate();
      actuator.execute(ret);

      //check DelegatedResourceAccountIndex convert
      ownerIndexCapsule = dbManager
          .getDelegatedResourceAccountIndexStore().get(ByteArray.fromHexString(OWNER_ADDRESS));
      Assert.assertNull(ownerIndexCapsule);

      ownerIndexCapsule = dbManager
          .getDelegatedResourceAccountIndexStore().getIndex(ByteArray.fromHexString(OWNER_ADDRESS));
      Assert.assertEquals(0, ownerIndexCapsule.getFromAccountsList().size());
      List<ByteString> tmpList = ownerIndexCapsule.getToAccountsList();
      Assert.assertEquals(RECEIVE_COUNT + 1, ownerIndexCapsule.getToAccountsList().size());
      for (int i = 0; i < RECEIVE_COUNT; i++) {
        Assert.assertEquals(beforeList.get(i), tmpList.get(i));
      }
      Assert.assertEquals(RECEIVE_ADDRESSES[RECEIVE_COUNT],
          ByteArray.toHexString(tmpList.get(RECEIVE_COUNT).toByteArray()));
    } catch (ContractValidateException | ContractExeException e) {
      Assert.fail("con not reach here");
    }
    dbManager.getDynamicPropertiesStore().saveAllowDelegateOptimization(0L);
    dbManager.getDynamicPropertiesStore().saveLatestBlockHeaderTimestamp(10000L);
  }
```
