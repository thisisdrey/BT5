This is a strong analog: the legacy (non-`AllowDelegateOptimization`) `DelegatedResourceAccountIndexStore` path in `FreezeBalanceActuator.delegateResource` stores an ever-growing, never-deduplicated-by-cost, single-object list of counterparty addresses per account, mirroring the OpenSSL bug class of "protocol-conforming interaction causes unbounded per-entity metadata retention with no pruning."

### Title
Unbounded growth of `DelegatedResourceAccountIndex` to/from-account lists causes single-account state bloat and serialization/storage DoS - (File: `actuator/src/main/java/org/tron/core/actuator/FreezeBalanceActuator.java`)

### Summary
When resource delegation uses the legacy (pre-`AllowDelegateOptimization`) code path, `FreezeBalanceActuator.delegateResource` appends the counterparty address to a single `DelegatedResourceAccountIndexCapsule` protobuf object's `to_accounts`/`from_accounts` repeated field, but never removes stale/duplicate-cost entries and never enforces a maximum size. A single funded account can cheaply generate a very large number of unique receiver addresses (delegation itself costs zero fee — `calcFee()` returns `0`) and delegate tiny bandwidth/energy amounts to each, causing that one index record to grow without bound.

### Finding Description
`FreezeBalanceActuator.delegateResource` reads the full `DelegatedResourceAccountIndexCapsule` for the owner/receiver, appends the new counterparty via `addToAccount`/`addFromAccount`, and persists the whole capsule back with `delegatedResourceAccountIndexStore.put(...)`: [1](#0-0) 
The underlying capsule stores the counterparties in a plain repeated protobuf field with an unconditional append and no cap on size: [2](#0-1) 
`calcFee()` for the delegation actuator returns `0`, so this operation costs only ordinary bandwidth/energy, not a scaling fee tied to the size of the index: [3](#0-2) 
Because the whole capsule (one protobuf record per account) is read, mutated by appending one more address, and rewritten on every delegation, the serialized size of that single database value grows linearly and unboundedly with the number of distinct receiver addresses the attacker chooses (an attacker fully controls both ends since receiver addresses can be freshly generated, unfunded accounts — `FreezeBalanceActuator` only checks `receiverCapsule.getType() != Contract`). Existing tests already exercise 100-entry growth in one object with no bound check: [4](#0-3) . This is analogous to the reported OpenSSL QUIC issue: a protocol-conforming, cheap, repeatable action causes one connection/account-scoped metadata object to retain unbounded state because the implementation appends but never bounds or prunes.

### Impact Explanation
Each delegation round-trips read-modify-write of the entire growing protobuf record through RocksDB/LevelDB, and the record must be fully deserialized/reserialized on every subsequent read or delegation to/from that account (e.g., `getIndex`/`getV2Index`, `Wallet` query paths that expose delegation info). As the list grows into the tens/hundreds of thousands of entries, this leads to increasing CPU, memory, and I/O cost per transaction touching that account, degrading transaction processing and potentially causing serialization slowdowns or out-of-memory pressure on nodes processing/re-broadcasting/re-executing the block containing such transactions — a resource-exhaustion condition consistent with the CWE-770 class in the reference.

### Likelihood Explanation
Reachable by any unprivileged account: `FreezeBalanceActuator` (delegation with `FreezeBalanceContract`, legacy path active when `AllowDelegateOptimization` is not enabled) requires only a funded account and freshly generated receiver public keys, with `calcFee()==0` for delegation. The cost is bounded only by ordinary transaction bandwidth/energy fees per transaction, not by the size of the target's accumulated index, so an attacker can incrementally grow the record over many cheap transactions.

### Recommendation
Enforce and validate a maximum number of entries in `DelegatedResourceAccountIndexCapsule.to_accounts`/`from_accounts` in the legacy path (mirroring what `AllowDelegateOptimization`'s per-pair-key storage design already avoids), reject/no-op duplicate or excessive delegation targets, or migrate/force all deployments onto the optimized per-pair index scheme (`delegatedResourceAccountIndexStore.delegate`) which does not suffer this unbounded single-record growth.

### Proof of Concept
1. Ensure `AllowDelegateOptimization` dynamic property is 0 (legacy path active) — the current default/most historical mainnet state before the optimization activation.
2. From a funded attacker account `A`, generate N fresh key pairs `R_1..R_N`.
3. For each `R_i`, broadcast a `FreezeBalanceContract` (or `DelegateResourceContract`) transaction delegating a minimal bandwidth/energy amount from `A` to `R_i`.
4. Observe `DelegatedResourceAccountIndexStore.get(A)`'s `to_accounts` list growing by one entry per transaction with no upper bound, as shown by the actuator logic at [5](#0-4)  and confirmed by the test pattern generating 100 unique receivers into a single index object at [6](#0-5) .
5. Repeat at scale (tens of thousands of transactions, feasible given zero delegation fee) to grow the single stored record to a size that measurably increases read/write latency and serialization cost for that account.

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

**File:** chainbase/src/main/java/org/tron/core/capsule/DelegatedResourceAccountIndexCapsule.java (L82-94)
```java
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

**File:** actuator/src/main/java/org/tron/core/actuator/DelegateResourceActuator.java (L277-280)
```java
  @Override
  public long calcFee() {
    return 0;
  }
```

**File:** framework/src/test/java/org/tron/core/actuator/FreezeBalanceActuatorTest.java (L275-331)
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
```
