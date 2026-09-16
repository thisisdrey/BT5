### Title
Unbounded per-account delegation index migration loop can exhaust transaction energy, permanently blocking resource delegation - (File: chainbase/src/main/java/org/tron/core/store/DelegatedResourceAccountIndexStore.java)

### Summary
The pSTAKE report describes a pattern where a per-call loop iterates over an unbounded, attacker-growable history of changes, causing the function to run out of gas once enough changes have accumulated. The same structural pattern exists in java-tron's legacy-to-V2 delegated-resource index migration: `DelegatedResourceAccountIndexStore.convert()` iterates the *entire* legacy `toAccounts`/`fromAccounts` list for an account in one atomic call, and that list can be grown to unbounded size by an ordinary account issuing many `FreezeBalance` (legacy) delegations to distinct receiver addresses over time.

### Finding Description
Legacy `FreezeBalanceActuator.delegateResource()` appends a new receiver/sender entry to `DelegatedResourceAccountIndexCapsule.toAccountsList` / `fromAccountsList` for every distinct counterpart address a user delegates to, with no upper bound on list length: [1](#0-0) 

These indexes are stored under the legacy (`FROM_PREFIX`/`TO_PREFIX`) key scheme. When the account later performs a V2 resource-delegation operation, the store lazily migrates the full legacy list to the V2 format via `convert()`, which iterates every entry in `toList` and `fromList` and re-writes two DB records per entry: [2](#0-1) 

This `convert()` call is invoked synchronously inside the TVM native-contract path for `DelegateResourceContract`/`UnDelegateResourceContract` processing: [3](#0-2) 

Because the number of legacy entries an account can accumulate is only bounded by how many distinct receiver/sender addresses the account chooses to freeze-delegate to (unbounded over the account's lifetime, analogous to an admin repeatedly changing the STokens reward rate), the single "conversion" transaction's cost grows linearly with the number of accumulated legacy delegation relationships, exactly mirroring the `_calculatePendingRewards` unbounded-iteration bug class.

### Impact Explanation
If an account accumulates a sufficiently large legacy delegation index (many distinct counterparties), the one-time `convert()` call triggered by its first V2 delegate/undelegate action can consume energy/CPU time proportional to that list size within a single transaction. If the required work exceeds the energy/fee limit or the per-transaction CPU time limit, the migration — and therefore any V2 resource delegation/undelegation for that account — can never complete, effectively locking the account out of using the new (and now default) resource-delegation model. This can result in resources/funds tied up in delegation relationships that can no longer be managed, a denial-of-service against a specific account's normal operation.

### Likelihood Explanation
Reaching this state requires no special privilege: any ordinary account can call the standard, publicly reachable `FreezeBalance`/delegate-to-many-receivers flow repeatedly (over time, across many blocks, each individually cheap) to grow their own legacy index, then later trigger `convert()` via a normal `DelegateResource`/`UnDelegateResource` call. This is directly analogous to the reported bug class (attacker/actor-controlled growth of an on-chain history structure that is later processed in one unbounded loop), making it a plausible, self-inflicted or third-party-triggerable DoS vector once accumulated past the point where a single conversion exceeds resource limits.

### Recommendation
Bound the number of legacy delegation index entries an account can accumulate (similar to the `UNFREEZE_MAX_TIMES` cap already used for `UnfreezeBalanceV2`), or make `convert()` incremental/paginated so it processes at most a fixed number of entries per transaction, persisting migration progress across multiple calls instead of requiring one all-or-nothing pass.

### Proof of Concept
Not independently executed; based on static code review only. Conceptually:
1. Using an account, repeatedly call the legacy `FreezeBalance` contract to delegate a small amount of bandwidth/energy to N distinct, attacker-controlled receiver addresses over many blocks (each call is cheap and independently valid), growing `toAccountsList` for the account to size N in `DelegatedResourceAccountIndexStore` legacy format.
2. Once N is large enough that iterating and re-writing 2×N `DelegatedResourceAccountIndexCapsule` records exceeds the available energy/fee limit or CPU time limit for a single transaction, call `DelegateResourceContract`/`UnDelegateResourceContract` (V2) for that account, which triggers `DelegatedResourceAccountIndexStore.convert()` over the full legacy list.
3. Observe the transaction failing due to out-of-energy/timeout, and that this failure is deterministic and unavoidable for that account under the V2 model going forward, since `convert()` is not incremental.

Verification of the exact N required and confirmation that no other guard limits legacy list growth was not fully completed due to tool-call limits; a Devin session with full repo/test access would be needed to empirically determine the exact threshold and confirm exploitability end-to-end.

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

**File:** actuator/src/main/java/org/tron/core/actuator/UnDelegateResourceActuator.java (L183-188)
```java
      //modify DelegatedResourceAccountIndexStore
      delegatedResourceAccountIndexStore.unDelegateV2(ownerAddress, receiverAddress);
    }

    accountStore.put(ownerAddress, ownerCapsule);

```
