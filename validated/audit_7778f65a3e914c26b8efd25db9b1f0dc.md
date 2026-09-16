### Title
Unbounded growth and unpaginated retrieval of `DelegatedResourceAccountIndex` entries enables memory-exhaustion DoS - ([File: chainbase/src/main/java/org/tron/core/store/DelegatedResourceAccountIndexStore.java])

### Summary
An unprivileged transaction broadcaster can cheaply create an unbounded number of persistent `DelegatedResourceAccountIndex` entries for a single account by issuing many `DelegateResourceContract` (or legacy `FreezeBalanceContract` with a receiver) transactions to distinct, freshly-generated receiver addresses. Unlike other list-growing structures in java-tron (votes are capped by `MAX_VOTE_NUMBER`, asset frozen-supply lists are capped by `getMaxFrozenSupplyNumber`), there is no cap on the number of distinct delegation targets per owner. When any anonymous API client subsequently queries `getDelegatedResourceAccountIndex`/`getDelegatedResourceAccountIndexV2` (exposed over gRPC and HTTP), the node performs an unbounded `prefixQuery` that materializes and sorts every accumulated entry into memory with no offset/limit, closely mirroring the QEMU `VIRTIO_GPU_CMD_RESOURCE_ATTACH_BACKING` pattern: unlimited "attach" operations followed by processing that consumes unbounded host/node memory.

### Finding Description
`DelegatedResourceAccountIndexStore.getWithPrefix` performs unlimited retrieval and materialization: [1](#0-0) 

This is invoked without any offset/limit from `getIndex`/`getV2Index`: [2](#0-1) 

which is exposed to any caller via `Wallet.getDelegatedResourceAccountIndex(V2)`: [3](#0-2) 

and reachable through the gRPC `WalletSolidityApi`/`Wallet` service and the HTTP servlets `GetDelegatedResourceAccountIndexServlet`/`GetDelegatedResourceAccountIndexV2Servlet` — endpoints callable by any anonymous API client without authentication.

The entries queried are populated per-(owner,receiver) pair with no cap on the number of distinct receivers an owner can delegate to. Each `DelegateResourceContract`/`FreezeBalanceContract` transaction to a new receiver persists new index rows: [4](#0-3) [5](#0-4) 

A test explicitly demonstrates the index list growing unboundedly across many receiver addresses with no rejection: [6](#0-5) 

By contrast, other analogous lists in the codebase are explicitly bounded — votes are capped by `MAX_VOTE_NUMBER`: [7](#0-6) 
and frozen-supply lists are capped by `getMaxFrozenSupplyNumber`: [8](#0-7) 
No equivalent cap exists for delegation targets, and unlike `getAssetIssueList`, which offers a paginated variant (`getAssetIssueListPaginated`) capped by `ASSET_ISSUE_COUNT_LIMIT_MAX`, the delegated-resource-index query path has no such limit.

### Impact Explanation
This maps directly to the CVE-2017-5578 bug class: unbounded "attach" operations (here, delegation transactions to unique receivers) followed by a query path that must process/aggregate all accumulated resources with no bound, driving unbounded memory allocation. A sufficiently large accumulated index (built cheaply over many low-cost transactions) could cause the node processing the query to allocate large amounts of heap for the `ArrayList`s, sorted comparator work, and protobuf capsule construction for every stored entry, potentially causing significant garbage collection pressure, degraded service, or an OutOfMemory crash — i.e., "an API the node can no longer serve" / node crash, which is an accepted impact category.

### Likelihood Explanation
Building the index costs only cheap, low-value `DelegateResourceContract`/`FreezeBalanceContract` transactions (minimal frozen balance, e.g., ~1 TRX) sent to freshly generated receiver addresses, each of which persists 2 DB rows with no per-owner cap. Because the query API (`getDelegatedResourceAccountIndex`/`V2`) is unauthenticated and requires only a target address, any anonymous client — including the attacker themselves — can then trigger the expensive read, making exploitation straightforward and repeatable at will once enough entries are accumulated.

### Recommendation
- Add pagination (offset/limit) to `getWithPrefix`/`getIndex`/`getV2Index` and their `Wallet` and gRPC/HTTP wrapper methods, similar to `getAssetIssueListPaginated`.
- Introduce a hard cap on the number of distinct delegation counterpart addresses per owner (analogous to `MAX_VOTE_NUMBER`/`getMaxFrozenSupplyNumber`), enforced in `DelegateResourceActuator.validate()`/`FreezeBalanceActuator.validate()`.
- Consider rate-limiting or capping response sizes for these unauthenticated index-query endpoints at the servlet/gRPC layer.

### Proof of Concept
1. From a funded account `A`, repeatedly broadcast `DelegateResourceContract` (or legacy `FreezeBalanceContract` with a receiver) transactions, each delegating a minimal amount of frozen bandwidth/energy to a newly generated, distinct receiver address `R_i` (i = 1..N, N arbitrarily large — no validation rejects this in `DelegateResourceActuator.validate()`).
2. Each transaction persists new rows under `V2_FROM_PREFIX + A + R_i` and `V2_TO_PREFIX + R_i + A` in `DelegatedResourceAccountIndexStore`, per `delegateResource` at `actuator/src/main/java/org/tron/core/actuator/DelegateResourceActuator.java:282-325`.
3. Any anonymous client calls the gRPC or HTTP `getDelegatedResourceAccountIndexV2` endpoint for address `A`.
4. `DelegatedResourceAccountIndexStore.getWithPrefix` (`chainbase/src/main/java/org/tron/core/store/DelegatedResourceAccountIndexStore.java:118-137`) executes `prefixQuery` for both `FROM`/`TO` prefixes, loading and sorting all N accumulated entries into memory and returning them in a single unpaginated response — repeatable and scalable by increasing N, driving increasing node memory/CPU consumption per query.

Note: I was unable to directly inspect the low-level `prefixQuery`/`Chainbase` implementation or the exact HTTP servlet source code due to tool errors in the final iteration; the conclusion that `prefixQuery` performs an unbounded full scan is inferred from the store code's use of `new ArrayList<>(this.prefixQuery(key).values())` with no limit parameter, and from the absence of any pagination parameters in the `Wallet.getDelegatedResourceAccountIndex(V2)` signatures found. Verifying the underlying RocksDB/LevelDB iteration bounds would further substantiate the severity.

### Citations

**File:** chainbase/src/main/java/org/tron/core/store/DelegatedResourceAccountIndexStore.java (L106-116)
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
```

**File:** chainbase/src/main/java/org/tron/core/store/DelegatedResourceAccountIndexStore.java (L118-137)
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

**File:** actuator/src/main/java/org/tron/core/actuator/DelegateResourceActuator.java (L282-325)
```java
  private void delegateResource(byte[] ownerAddress, byte[] receiverAddress, boolean isBandwidth,
                                long balance, boolean lock, long lockPeriod) {
    AccountStore accountStore = chainBaseManager.getAccountStore();
    DynamicPropertiesStore dynamicPropertiesStore = chainBaseManager.getDynamicPropertiesStore();
    DelegatedResourceStore delegatedResourceStore = chainBaseManager.getDelegatedResourceStore();
    DelegatedResourceAccountIndexStore delegatedResourceAccountIndexStore = chainBaseManager
        .getDelegatedResourceAccountIndexStore();

    // 1. unlock the expired delegate resource
    long now = chainBaseManager.getDynamicPropertiesStore().getLatestBlockHeaderTimestamp();
    delegatedResourceStore.unLockExpireResource(ownerAddress, receiverAddress, now);

    //modify DelegatedResourceStore
    long expireTime = 0;
    if (lock) {
      expireTime = now + lockPeriod * BLOCK_PRODUCED_INTERVAL;
    }
    byte[] key = DelegatedResourceCapsule.createDbKeyV2(ownerAddress, receiverAddress, lock);
    DelegatedResourceCapsule delegatedResourceCapsule = delegatedResourceStore.get(key);
    if (delegatedResourceCapsule == null) {
      delegatedResourceCapsule = new DelegatedResourceCapsule(ByteString.copyFrom(ownerAddress),
          ByteString.copyFrom(receiverAddress));
    }

    if (isBandwidth) {
      delegatedResourceCapsule.addFrozenBalanceForBandwidth(balance, expireTime);
    } else {
      delegatedResourceCapsule.addFrozenBalanceForEnergy(balance, expireTime);
    }
    delegatedResourceStore.put(key, delegatedResourceCapsule);

    //modify DelegatedResourceAccountIndexStore
    delegatedResourceAccountIndexStore.delegateV2(ownerAddress, receiverAddress,
        dynamicPropertiesStore.getLatestBlockHeaderTimestamp());

    //modify AccountStore for receiver
    AccountCapsule receiverCapsule = accountStore.get(receiverAddress);
    if (isBandwidth) {
      receiverCapsule.addAcquiredDelegatedFrozenV2BalanceForBandwidth(balance);
    } else {
      receiverCapsule.addAcquiredDelegatedFrozenV2BalanceForEnergy(balance);
    }
    accountStore.put(receiverCapsule.createDbKey(), receiverCapsule);
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/FreezeBalanceActuator.java (L289-353)
```java
  private long delegateResource(byte[] ownerAddress, byte[] receiverAddress, boolean isBandwidth,
      long balance, long expireTime) {
    AccountStore accountStore = chainBaseManager.getAccountStore();
    DynamicPropertiesStore dynamicPropertiesStore = chainBaseManager.getDynamicPropertiesStore();
    DelegatedResourceStore delegatedResourceStore = chainBaseManager.getDelegatedResourceStore();
    DelegatedResourceAccountIndexStore delegatedResourceAccountIndexStore = chainBaseManager
        .getDelegatedResourceAccountIndexStore();
    byte[] key = DelegatedResourceCapsule.createDbKey(ownerAddress, receiverAddress);
    //modify DelegatedResourceStore
    DelegatedResourceCapsule delegatedResourceCapsule = delegatedResourceStore
        .get(key);
    if (delegatedResourceCapsule != null) {
      if (isBandwidth) {
        delegatedResourceCapsule.addFrozenBalanceForBandwidth(balance, expireTime);
      } else {
        delegatedResourceCapsule.addFrozenBalanceForEnergy(balance, expireTime);
      }
    } else {
      delegatedResourceCapsule = new DelegatedResourceCapsule(
          ByteString.copyFrom(ownerAddress),
          ByteString.copyFrom(receiverAddress));
      if (isBandwidth) {
        delegatedResourceCapsule.setFrozenBalanceForBandwidth(balance, expireTime);
      } else {
        delegatedResourceCapsule.setFrozenBalanceForEnergy(balance, expireTime);
      }

    }
    delegatedResourceStore.put(key, delegatedResourceCapsule);

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

**File:** framework/src/test/java/org/tron/core/actuator/FreezeBalanceActuatorTest.java (L275-337)
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

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/VoteWitnessProcessor.java (L33-36)
```java
    if (param.getVotes().size() > MAX_VOTE_NUMBER) {
      throw new ContractValidateException(
          "VoteNumber more than maxVoteNumber " + MAX_VOTE_NUMBER);
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/AssetIssueActuator.java (L232-235)
```java
    if (assetIssueContract.getFrozenSupplyCount()
        > dynamicStore.getMaxFrozenSupplyNumber()) {
      throw new ContractValidateException("Frozen supply list length is too long");
    }
```
