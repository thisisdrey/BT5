## Finding: Unbounded delegated-resource index lists enable O(n²) transaction cost and unbounded state/API growth

### Title
Unbounded growth of `DelegatedResourceAccountIndexCapsule.toAccounts`/`fromAccounts` lists causes quadratic-cost transactions and unbounded API payloads - ([File: actuator/src/main/java/org/tron/core/actuator/FreezeBalanceActuator.java])

### Summary
`FreezeBalanceActuator` (legacy freeze-with-delegate path) and `DelegatedResourceAccountIndexStore` maintain, per account, an unbounded `ByteString` list of every distinct delegation counterpart ever seen. Neither `validate()` nor `execute()` enforces any cap on the size of this list, and several operations iterate the *entire* list on every call, giving an attacker a way to grow per-account state without bound and make follow-on operations progressively more expensive — the same "unbounded loop" bug class flagged in the referenced Carapace report.

### Finding Description
When `FreezeBalanceActuator` delegates resources under the legacy (non-optimized) index format, it appends the counterpart address to an in-memory list and checks for duplicates with a linear scan, with no upper bound on list size: [1](#0-0) 

Each call to `delegateResource()` does an O(n) `.contains()` check against the existing `toAccountsList`/`fromAccountsList` before appending, so building up a list of `n` distinct counterparties costs O(n²) total, and the list itself (`DelegatedResourceAccountIndexCapsule`) has no maximum-size validation anywhere in `FreezeBalanceActuator.validate()`.

Once the "delegate optimization" fork is active, any further legacy-format delegation from the same address triggers a one-time migration that iterates the *entire* accumulated list: [2](#0-1) 

`convert()` is invoked directly from `FreezeBalanceActuator.delegateResource()`: [3](#0-2) 

For every entry in the pre-existing (unbounded) `toList`/`fromList`, `convert()` performs a `delegate()` call, which itself does two additional store writes — so this single transaction's cost scales linearly with however many counterparties the attacker previously accumulated, with no limit.

Separately, `getWithPrefix()` (used by `getIndex`/`getV2Index`) performs an unbounded `prefixQuery` scan and sort over all index entries for an address: [4](#0-3) 

This is directly reachable from the read-only Wallet API surface (`GetDelegatedResourceAccountIndex`/`GetDelegatedResourceAccountIndexV2` via gRPC/HTTP), which returns the entire unbounded list to the caller: [5](#0-4) 

By contrast, other resource-list constructs in the codebase (`UnfreezeBalanceV2Actuator.UNFREEZE_MAX_TIMES = 32`, `VoteWitnessActuator.MAX_VOTE_NUMBER`) are explicitly capped, showing the project's own convention is to bound these lists — but the delegated-resource-index list is not bounded.

### Impact Explanation
An attacker who controls a single account (no special privileges required) can:
1. Freeze a small balance and repeatedly call `FreezeBalance` with a resource delegation to a large number of distinct, self-controlled receiver addresses (legacy/non-optimized index mode). Each call is a normal signed transaction, so the attacker pays for it but at a modest, predictable per-tx cost — while permanently growing an unbounded on-chain list.
2. Trigger `convert()` afterward (once delegate optimization activates) to force a single transaction whose cost scales linearly (unboundedly) with the previously built-up list size, potentially exceeding block/gas limits or making the transaction fail deterministically for that account, effectively getting the account's frozen/delegated funds "stuck" until manual intervention, similar to the impact described in the original report (locked funds / broken protocol logic due to unbounded iteration).
3. Query `GetDelegatedResourceAccountIndex`/`V2` via the public gRPC/HTTP API for that address to force the node to scan and serialize an arbitrarily large list on every request, degrading node responsiveness for that query path (API-level resource exhaustion), reachable by any anonymous API client without authentication.

### Likelihood Explanation
Likelihood is moderate-to-high: no special account permissions or fork-specific races are needed, and the actions (repeated `FreezeBalance` with delegation to many addresses, or repeated read-only index queries) are ordinary transaction/API calls available to any user. The attacker does bear transaction fees to grow the list, but the fee scales linearly per addition (not with the eventual O(n) `convert()`/query cost), so the attack is economically incentivized to shift a disproportionate cost onto a single future transaction or onto node query-serving capacity.

### Recommendation
- Enforce an explicit maximum size (analogous to `UNFREEZE_MAX_TIMES`/`MAX_VOTE_NUMBER`) on `DelegatedResourceAccountIndexCapsule.toAccounts`/`fromAccounts`, rejecting further delegations in `FreezeBalanceActuator.validate()`/`DelegateResourceActuator.validate()` once the cap is reached.
- Avoid O(n) `.contains()` scans on every delegation by using a set-backed structure or separate per-pair keys (which the "V2"/optimized format already does — consider deprecating/migrating the legacy format entirely).
- Bound or paginate `getWithPrefix()`/`prefixQuery` results returned by the `GetDelegatedResourceAccountIndex` API, or require pagination parameters from the caller.
- Consider bounding or batching `convert()` so migration work is spread across multiple transactions rather than performed atomically in one.

### Proof of Concept
1. Deploy/control account `A`, freeze balance, and repeatedly call `FreezeBalanceContract` with `resource_receiver=A`→`R1..Rn` (n distinct freshly generated receiver addresses) while `supportAllowDelegateOptimization()` is false — each call appends to `A`'s `toAccountsList` with an O(n) duplicate check, growing the list unboundedly at attacker-controlled but ever-increasing per-tx cost.
2. Once the delegate-optimization fork activates, submit one more `FreezeBalanceContract` delegation from `A`; this triggers `DelegatedResourceAccountIndexStore.convert(A)`, which iterates the entire accumulated `toAccountsList`/`fromAccountsList` (size n) inside a single transaction execution — cost grows linearly/unboundedly with n and is not capped anywhere in validation.
3. Call `Wallet.getDelegatedResourceAccountIndex(A)` (exposed via gRPC/HTTP `GetDelegatedResourceAccountIndex`) repeatedly; each call performs an unbounded `prefixQuery` + sort over all of `A`'s index entries, imposing unbounded per-request cost on the serving node.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/FreezeBalanceActuator.java (L322-345)
```java
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

**File:** actuator/src/main/java/org/tron/core/actuator/FreezeBalanceActuator.java (L347-353)
```java
    } else {
      // modify DelegatedResourceAccountIndexStore new
      delegatedResourceAccountIndexStore.convert(ownerAddress);
      delegatedResourceAccountIndexStore.convert(receiverAddress);
      delegatedResourceAccountIndexStore.delegate(ownerAddress, receiverAddress,
          dynamicPropertiesStore.getLatestBlockHeaderTimestamp());
    }
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
