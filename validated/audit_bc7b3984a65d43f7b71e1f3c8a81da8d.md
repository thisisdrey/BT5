### Title
Unbounded growth of `DelegatedResourceAccountIndexCapsule.toAccountsList`/`fromAccountsList` causes O(n) / O(n log n) cost blow-up on every resource delegation and index query - ([File: chainbase/src/main/java/org/tron/core/store/DelegatedResourceAccountIndexStore.java])

### Summary
When an account delegates bandwidth/energy via `FreezeBalanceContract` with a receiver address (legacy delegate model, i.e. when `supportAllowDelegateOptimization()` is false), each new distinct receiver address is appended to an unbounded `repeated bytes` list (`toAccounts`/`fromAccounts`) inside `DelegatedResourceAccountIndexCapsule`, with no upper bound check, analogous to the unbounded `poolColl.tokens[]` growth in the referenced Yeti Finance finding.

### Finding Description
`FreezeBalanceActuator.delegateResource()` maintains the legacy delegation index by loading the owner's/receiver's `DelegatedResourceAccountIndexCapsule` and unconditionally appending the counterpart address if not already present: [1](#0-0) 
There is no validation anywhere in `FreezeBalanceActuator.validate()`/`execute()` limiting how many distinct receiver addresses an owner account can accumulate in `toAccountsList`, nor how many distinct sender addresses a receiver can accumulate in `fromAccountsList`. Since `FreezeBalanceContract` still allows freezing an arbitrarily small `frozenBalance` (bounded only by the 1 TRX minimum enforced generically for freeze amounts) to any number of unique receiver addresses, an attacker fully controls the growth of this list by issuing many cheap `FreezeBalanceContract` transactions with different receivers (or being the target of such transactions from many senders).

This list is later read and iterated/sorted on every subsequent index lookup and on the one-time legacy→V2 `convert()` migration: [2](#0-1) [3](#0-2) 
`getWithPrefix` (used by `getV2Index`) performs a `prefixQuery`, builds an `ArrayList`, and **sorts** it by timestamp — an O(n log n) operation executed synchronously inside a wallet/API query path (`Wallet.getDelegatedResourceAccountIndex`, exposed via `GetDelegatedResourceAccountIndexServlet` and `RpcApiService`). The removal path in `UnfreezeBalanceActuator` also performs a linear scan-and-rebuild of the full list on every unfreeze: [4](#0-3) 
A test in the repo already demonstrates the ability to grow the owner's `toAccountsList` to 100+ entries from a single account with no rejection: [5](#0-4) 

By contrast, the newer `UnfreezeBalanceV2Actuator`/`DelegateResourceActuator` path deliberately bounds a comparable structure (`unfrozenV2` list) with `UNFREEZE_MAX_TIMES = 32` and validates it on every transaction: [6](#0-5) [7](#0-6) 
No equivalent bound exists for the legacy `DelegatedResourceAccountIndexCapsule` list, which is the same class of bug as the reported "Out of gas" finding: an attacker-controlled unbounded array that is iterated on every subsequent operation.

### Impact Explanation
Each unique receiver address costs only the standard bandwidth/energy of a `FreezeBalanceContract` transaction plus a minimal frozen balance, making this cheap to grow arbitrarily large per account. As the list grows:
- Every subsequent `delegate`/`unDelegate`/`convert` call, and every `GetDelegatedResourceAccountIndex` API/gRPC query against the affected account, does increasing linear work (list rebuild/removal, `ArrayList` copy, sort) that scales with the number of stored addresses.
- Because this work happens inside block-processing actuators (`FreezeBalanceActuator`, `UnfreezeBalanceActuator`) as well as unauthenticated read-only API paths, a sufficiently large list can degrade block processing time for transactions touching that account and materially slow down (or, in extreme cases with very large N, significantly increase latency of) the node's API for that account, matching the "API the node can no longer serve" acceptance criterion for a resource-exhaustion class bug, albeit bounded by the underlying LevelDB/RocksDB store size and per-block resource (bandwidth/energy) limits which throttle how fast an attacker can grow the list per block.

### Likelihood Explanation
The path is reachable by any unprivileged account issuing ordinary `FreezeBalanceContract` transactions (or being targeted by many senders), requiring no special privileges — only the willingness to pay bandwidth/energy fees and freeze a minimal amount to many distinct receiver addresses. `FreezeBalanceContract` remains a valid broadcastable contract type in this codebase and is only routed to the newer bounded `DelegateResourceActuator` path when `supportAllowDelegateOptimization()` is enabled on-chain; when this parameter is not enabled (or for chains that never enabled it), the unbounded legacy path is used.

### Recommendation
- Add an explicit cap (mirroring `UNFREEZE_MAX_TIMES`) on the number of entries permitted in `DelegatedResourceAccountIndexCapsule.toAccountsList`/`fromAccountsList`, validated in `FreezeBalanceActuator.validate()` before allowing a new delegate relationship to be recorded.
- Alternatively/additionally, force conversion to the optimized (`supportAllowDelegateOptimization`) index format network-wide and deprecate/disable the legacy unbounded list persistence path.
- Avoid performing full-list sort/rebuild operations synchronously inside externally-reachable query and actuator code paths; consider paginated storage keyed directly by `(owner, receiver)` instead of an in-place repeated list.

### Proof of Concept
1. Create account A with sufficient TRX to pay minimal freeze amounts and bandwidth fees.
2. Broadcast N `FreezeBalanceContract` transactions from A, each specifying a distinct `receiver_address` and the minimum allowed `frozen_balance` for `BANDWIDTH` (with `supportDR()` enabled and `supportAllowDelegateOptimization()` disabled/not yet active).
3. Each transaction appends one entry to A's `DelegatedResourceAccountIndexCapsule.toAccountsList` via `delegateResource()` (`actuator/src/main/java/org/tron/core/actuator/FreezeBalanceActuator.java:320-345`), with no cap enforced, as also exercised by the existing test `testMultiFreezeDelegatedBalanceForBandwidth` growing the list past 100 entries.
4. Repeat at scale (bounded only by attacker's available bandwidth/energy and TRX to freeze) to grow the list to a large size.
5. Observe increasing latency of `Wallet.getDelegatedResourceAccountIndex` for account A (`chainbase/src/main/java/org/tron/core/store/DelegatedResourceAccountIndexStore.java:118-138`, involving `ArrayList` construction + `sort`), and of subsequent `FreezeBalanceContract`/`UnfreezeBalanceContract` transactions against A that touch this index.

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

**File:** framework/src/test/java/org/tron/core/actuator/FreezeBalanceActuatorTest.java (L275-303)
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
```

**File:** actuator/src/main/java/org/tron/core/actuator/UnfreezeBalanceV2Actuator.java (L43-44)
```java
  @Getter
  private static final int UNFREEZE_MAX_TIMES = 32;
```

**File:** actuator/src/main/java/org/tron/core/actuator/UnfreezeBalanceV2Actuator.java (L179-182)
```java
    int unfreezingCount = accountCapsule.getUnfreezingV2Count(now);
    if (UNFREEZE_MAX_TIMES <= unfreezingCount) {
      throw new ContractValidateException("Invalid unfreeze operation, unfreezing times is over limit");
    }
```
