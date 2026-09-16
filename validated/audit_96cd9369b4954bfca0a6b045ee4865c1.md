### Title
Unbounded growth of `toAccounts`/`fromAccounts` repeated fields in `DelegatedResourceAccountIndexCapsule` enables DoS via `FreezeBalanceContract` - (File: actuator/src/main/java/org/tron/core/actuator/FreezeBalanceActuator.java)

### Summary
`FreezeBalanceActuator.delegateResource` maintains a per-account index (`DelegatedResourceAccountIndexCapsule`) whose `toAccounts`/`fromAccounts` repeated fields grow without any bound when a user freezes-and-delegates TRX to new receiver addresses under the legacy (pre-`AllowDelegateOptimization`) code path. This mirrors the reported `lockExtensions` unbounded-array pattern: a single, unprivileged, repeatable transaction type (`FreezeBalanceContract`) keeps appending to an array stored inside one protobuf value with no cap, and every append/removal does an O(n) linear scan (`contains()`/`remove()`), degrading over time and bloating the stored/read value.

### Finding Description
When `dynamicStore.supportDR()` is true and a receiver address is specified in `FreezeBalanceContract`, `FreezeBalanceActuator.delegateResource` is invoked: [1](#0-0) 

Inside `delegateResource`, if the network has **not** enabled `supportAllowDelegateOptimization`, the legacy index-maintenance branch executes: [2](#0-1) 

For every new, distinct receiver address the owner delegates to, `ownerIndexCapsule.addToAccount(...)` appends an entry to `toAccounts`, and the receiver's capsule similarly grows `fromAccounts` via `addFromAccount(...)`. Neither field has a size limit: [3](#0-2) 

The check to avoid duplicate entries (`toAccountsList.contains(...)`) is a linear scan over the entire array, so each additional freeze-and-delegate call costs proportional time to the array's current size — this is essentially the same "unbounded array + O(n) work per append" pattern as `lockExtensions.push(...)` in the reported Solidity bug. The full capsule (containing the whole array) is read and rewritten to the DB on every call: [4](#0-3) 

The same growing array is also read back on `UnfreezeBalanceActuator` unfreeze, which performs another O(n) `remove()` on a copied `ArrayList`: [5](#0-4) 

Additionally, this capsule is directly exposed via the public/unauthenticated gRPC and HTTP query APIs (`getDelegatedResourceAccountIndex`), which fetch and serialize the entire stored array to respond to any caller: [6](#0-5) 

An attacker only needs balance sufficient to freeze the minimum bandwidth/energy amount to a fresh receiver address (which need not even be a real, funded account) repeatedly, each time creating a new distinct entry, since the dedupe check is by exact address match. There is no cap on the number of distinct receiver addresses one account can delegate to, and no fee scaling with the size of this growing array.

### Impact Explanation
As `toAccounts`/`fromAccounts` grow large:
- Each subsequent `FreezeBalanceContract` (delegate) transaction from the same owner becomes progressively more expensive to execute (linear-scan dedupe check, full-value read/write), increasing actuator execution time and increasing pressure on block processing when many such transactions are packed into blocks.
- Any subsequent `UnfreezeBalanceContract` transaction targeting this account also performs O(n) copy+remove work.
- Public/unauthenticated `getDelegatedResourceAccountIndex`/`getDelegatedResourceAccountIndexV2` gRPC/HTTP query calls (reachable by any anonymous API client) will read and serialize the entire array, which can be made arbitrarily large by the attacker beforehand, resulting in oversized responses and increased CPU/memory/bandwidth usage on the serving node — a resource-exhaustion vector against the node's query-serving capability.
- Because this data structure is persisted per-block via `TronStoreWithRevoking`, it also increases per-block store I/O size for that key indefinitely.

This does not directly cause theft of funds, but it degrades an actuator's execution cost and a node's API-serving capability in a way that scales with attacker-controlled input and has no bound — consistent with the "DoS an API the node can no longer serve" / degraded actuator execution class called out in scope.

### Likelihood Explanation
This legacy code path is only active while `dynamicPropertiesStore.supportAllowDelegateOptimization()` is false on the given network. On networks/testnets or older-configured private chains where this parameter (`ALLOW_DELEGATE_OPTIMIZATION`, an on-chain committee proposal) has not been activated, any account holder can trigger unbounded growth simply by repeatedly issuing minimal-value `FreezeBalanceContract` transactions to new receiver addresses — a normal, unprivileged operation requiring only a valid signed transaction and minimal TRX to freeze. I was unable to verify from the available index whether `ALLOW_DELEGATE_OPTIMIZATION` is hard-activated (default enabled) on current mainnet in this codebase snapshot, or whether it remains a committee-toggleable parameter that could be off on some chains; this affects the practical reachability of the vulnerable branch and should be confirmed against the live/default `DynamicPropertiesStore` values.

### Recommendation
- Impose a hard cap on the number of entries in `toAccounts`/`fromAccounts` per `DelegatedResourceAccountIndexCapsule` (mirroring the cap already applied elsewhere, e.g., `getAvailableUnfreezeCount`), rejecting further delegate operations to new receivers beyond the cap, or requiring a fee proportional to array size.
- Replace the linear `contains()`/`remove()` scans with a bounded/O(1) structure, or migrate all data fully to the key-per-(from,to)-pair model (already used by `supportAllowDelegateOptimization`/V2), and force this migration/hard-enable it network-wide rather than leaving the unbounded legacy path reachable.
- Ensure `getDelegatedResourceAccountIndex`/`V2` API responses are paginated or size-limited to prevent large-array serialization from being used as an amplification/DoS vector.

### Proof of Concept
1. On a chain where `supportAllowDelegateOptimization()` is false (or reachable via configuration), attacker controls address `A` with funds sufficient for minimum freeze increments.
2. Attacker repeatedly submits `FreezeBalanceContract` transactions from `A`, each time targeting a newly generated, distinct `receiverAddress_i`, with resource `BANDWIDTH` or `ENERGY`, minimal `frozen_balance`.
3. Each transaction executes `FreezeBalanceActuator.delegateResource`, appending `receiverAddress_i` to `A`'s `DelegatedResourceAccountIndexCapsule.toAccounts` (unbounded growth) and appending `A` to each `receiverAddress_i`'s `fromAccounts`.
4. After N such transactions, `A`'s stored index capsule contains N entries; subsequent freeze/unfreeze/delegate operations on `A` incur O(N) work, and the existing test `testMultiFreezeDelegatedBalanceForBandwidth` demonstrates the mechanics of this growing list under normal (non-malicious) conditions with 100 entries — an attacker can scale N arbitrarily higher: [7](#0-6) 
5. An anonymous API client calls `getDelegatedResourceAccountIndex(A)` via gRPC/HTTP, forcing the node to read and serialize the entire N-entry array in a single response, consuming disproportionate node resources relative to the request cost.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/FreezeBalanceActuator.java (L79-96)
```java
    long increment;
    switch (freezeBalanceContract.getResource()) {
      case BANDWIDTH:
        if (!ArrayUtils.isEmpty(receiverAddress)
            && dynamicStore.supportDR()) {
          increment = delegateResource(ownerAddress, receiverAddress, true,
                  frozenBalance, expireTime);
          accountCapsule.addDelegatedFrozenBalanceForBandwidth(frozenBalance);
        } else {
          long oldNetWeight = accountCapsule.getFrozenBalance() / TRX_PRECISION;
          long newFrozenBalanceForBandwidth =
              frozenBalance + accountCapsule.getFrozenBalance();
          accountCapsule.setFrozenForBandwidth(newFrozenBalanceForBandwidth, expireTime);
          long newNetWeight = accountCapsule.getFrozenBalance() / TRX_PRECISION;
          increment = newNetWeight - oldNetWeight;
        }
        addTotalWeight(BANDWIDTH, dynamicStore, frozenBalance, increment);
        break;
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

**File:** chainbase/src/main/java/org/tron/core/capsule/DelegatedResourceAccountIndexCapsule.java (L46-86)
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
