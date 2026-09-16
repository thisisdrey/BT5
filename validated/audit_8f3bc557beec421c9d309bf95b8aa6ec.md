Based on the investigation, the closest analog to CVE-2018-14660 (repeated attribute-triggered lock creation causing unbounded per-object growth and memory exhaustion) in this codebase is the legacy delegated-resource indexing path in `FreezeBalanceActuator`.

### Title
Unbounded growth of `DelegatedResourceAccountIndex` via repeated `FreezeBalanceContract` delegation causes per-account memory/storage exhaustion - (File: actuator/src/main/java/org/tron/core/actuator/FreezeBalanceActuator.java)

### Summary
The GlusterFS CVE allowed an authenticated user to repeatedly call `setxattr` with `GF_META_LOCK_KEY`, appending an unbounded number of locks to a single inode's internal structure, exhausting server memory. The structural analog in java-tron is `FreezeBalanceActuator.delegateResource()`, which appends an unbounded number of entries to a single account's `DelegatedResourceAccountIndex` (`toAccounts`/`fromAccounts` lists) whenever the "legacy" (non-optimized) delegation code path is active.

### Finding Description
When a user broadcasts a `FreezeBalanceContract` with a `receiverAddress` set (i.e., delegating bandwidth/energy) and `dynamicStore.supportAllowDelegateOptimization()` is false, execution goes through the non-optimized branch: [1](#0-0) 

Each call with a new, distinct `receiverAddress` appends one more entry into `ownerIndexCapsule`'s `toAccountsList` (and correspondingly the receiver's `fromAccountsList`) via `DelegatedResourceAccountIndexCapsule.addToAccount` / `addFromAccount`: [2](#0-1) 

There is no cap on the number of distinct receiver addresses one owner account can delegate to. The only per-transaction constraints checked in `validate()` are on `frozenBalance` (minimum 1 TRX) and the owner's *own* `frozenCount` (0 or 1), neither of which limits the number of distinct delegation relationships: [3](#0-2) 

A test in the repository already demonstrates the list growing arbitrarily as more receivers are delegated to, confirming this is unbounded by design in the legacy path: [4](#0-3) 

Each `DelegatedResourceAccountIndex` protobuf is stored as a single value under one key (the owner's address) in the account index store, so the entire (unbounded) list must be deserialized as one unit whenever it is read — e.g., by `Wallet`/gRPC/HTTP APIs (`getDelegatedResourceAccountIndex`), or by later `FreezeBalance`/`UnfreezeBalance` actuator executions, or block replay during sync.

### Impact Explanation
By repeatedly broadcasting minimal-cost `FreezeBalanceContract` transactions (minimum 1 TRX per delegation, to a fresh receiver address each time), an attacker can grow their own `DelegatedResourceAccountIndex` entry (and pollute many receivers' `fromAccounts` lists) without limit. Because each read of this capsule (via API queries, subsequent freeze/unfreeze operations, or block application) deserializes the full list into memory, sufficiently large lists can cause excessive memory allocation on any full node processing that data, potentially leading to node slowdown or crash — matching the "server memory exhaustion from unbounded per-object structure growth" root cause of CVE-2018-14660.

### Likelihood Explanation
This path is only reachable when `supportAllowDelegateOptimization()` is false — i.e., on chains/testnets where this dynamic parameter has not been activated by the committee (the parameter is a committee-controlled proposal, not universally on for all deployments). Where active, any account holder can trigger it with only the minimum 1 TRX freeze amount per delegation and gas/bandwidth cost of a normal transaction — no special privilege required beyond having a funded account, satisfying the "unprivileged transaction broadcaster" reachability bar.

### Recommendation
Add an explicit maximum cap on the number of distinct entries allowed in `DelegatedResourceAccountIndexCapsule.toAccountsList`/`fromAccountsList` in the legacy (non-optimized) delegation path in `FreezeBalanceActuator.delegateResource()`, analogous to the `UNFREEZE_MAX_TIMES` cap already used in `UnfreezeBalanceV2Actuator`. Alternatively, force migration to the per-relationship-key (`convert`/`delegate`) storage model unconditionally, regardless of the `AllowDelegateOptimization` flag state, to avoid single-key unbounded growth entirely.

### Proof of Concept
1. Deploy/target a chain where `supportAllowDelegateOptimization()` returns false (default before that proposal is enabled).
2. From a funded account `A`, repeatedly broadcast `FreezeBalanceContract` transactions delegating 1 TRX of BANDWIDTH each to a newly generated receiver address `R_1, R_2, ..., R_n`.
3. Each transaction appends one entry to `A`'s `DelegatedResourceAccountIndex.toAccounts` list and one entry to each `R_i`'s `fromAccounts` list, as shown by `FreezeBalanceActuatorTest.testMultiFreezeDelegatedBalanceForBandwidth` scaling with `RECEIVE_COUNT`.
4. Repeating this at scale (thousands to millions of iterations) grows a single stored value unboundedly; subsequent reads/deserialization of that key (via Wallet API queries or further freeze/unfreeze operations touching that account) consume proportionally large memory, potentially exhausting node memory.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/FreezeBalanceActuator.java (L187-201)
```java
    long frozenBalance = freezeBalanceContract.getFrozenBalance();
    if (frozenBalance <= 0) {
      throw new ContractValidateException("frozenBalance must be positive");
    }
    if (frozenBalance < TRX_PRECISION) {
      throw new ContractValidateException("frozenBalance must be greater than or equal to 1 TRX");
    }

    int frozenCount = accountCapsule.getFrozenCount();
    if (!(frozenCount == 0 || frozenCount == 1)) {
      throw new ContractValidateException("frozenCount must be 0 or 1");
    }
    if (frozenBalance > accountCapsule.getBalance()) {
      throw new ContractValidateException("frozenBalance must be less than or equal to accountBalance");
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

**File:** chainbase/src/main/java/org/tron/core/capsule/DelegatedResourceAccountIndexCapsule.java (L57-94)
```java
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
