### Title
Unbounded, linear-scanned delegation index enables per-transaction O(n) cost with no cap on entries - ([File: actuator/src/main/java/org/tron/core/actuator/DelegateResourceActuator.java])

### Summary
The `SegmentSmack`-shape bug in the report (cheap, small inputs that force a full O(n) re-scan/re-fold of an unbounded per-connection structure) has a structural analog in java-tron's legacy `DelegatedResourceAccountIndexStore` index path, used by `FreezeBalanceActuator.delegateResource()` and by `DelegateResourceActuator`/`DelegateResourceProcessor` when the "delegate optimization" hard fork is not yet active. Each call to freeze-and-delegate (or delegate) resources to a new receiver address appends to a flat, protobuf-encoded `repeated bytes` list (`toAccountsList` / `fromAccountsList`) stored in a single `DelegatedResourceAccountIndexCapsule`, after doing a full linear `.contains()` scan of that same list to de-duplicate.

### Finding Description
In `FreezeBalanceActuator.delegateResource()`, when `dynamicPropertiesStore.supportAllowDelegateOptimization()` is false (legacy code path, still compiled and reachable on chains/configs that have not activated that hard-fork switch), each delegate operation does: [1](#0-0) 

The list is read back and re-serialized in full on every insertion via `DelegatedResourceAccountIndexCapsule.addToAccount` / `addFromAccount`, each of which rebuilds the whole protobuf message: [2](#0-1) 

There is no cap on the number of distinct receiver/from addresses that can be appended to a single owner's index capsule. An attacker who controls (or generates) many distinct receiver addresses can call `FreezeBalanceContract`/`DelegateResourceContract` repeatedly (freezing/delegating the minimum 1 TRX each time to a fresh receiver address) and each call forces:
1. A full deserialization of the account's growing `DelegatedResourceAccountIndex` protobuf,
2. An O(n) `List.contains()` scan over the accumulated list to check for duplicates,
3. A full re-serialization (`toBuilder().addToAccounts(...).build()`) of the entire, ever-growing message,
4. A `put()` of this whole growing blob back into the revoking KV store.

This mirrors the reported bug class exactly: a small, cheap, valid transaction that appends one more "segment" (an address) to an unbounded, per-account queue, and every subsequent operation re-processes/re-serializes the entire queue rather than doing O(1) or O(log n) work. Unlike other bounded per-account lists found in the same audit (`FrozenList`/`UnfreezeV2List` capped at 32 entries via `UNFREEZE_MAX_TIMES`, market orders capped at `MAX_ACTIVE_ORDER_NUM`/`MAX_MATCH_NUM`), this delegation index list has no such cap in the legacy path.

### Impact Explanation
Each freeze-with-delegate transaction costs the attacker only ~1 TRX (the minimum freezable amount) plus bandwidth/energy fees, and the balance is later recoverable via unfreeze. A single attacker account can, over many blocks, build a delegation index list of thousands of entries; every further delegate/undelegate/freeze/unfreeze targeting that same owner then incurs O(n) CPU work for deserialization, linear scan, and re-serialization of that specific owner's index record, and the record itself (and, transiently, its serialized bytes) grows unbounded, imposing memory and processing cost on every full node applying such blocks. Because this runs inside `Manager`/actuator block application, sustained abuse degrades block-application throughput for the whole network processing chain (denial-of-service against block execution), matching the CVSS Availability-only impact of the reference bug.

### Likelihood Explanation
Reachability requires only ordinary `FreezeBalanceContract` transactions with a receiver address (an unprivileged, permissionless broadcaster action) and depends on the legacy (`supportAllowDelegateOptimization() == false`) code path still being active — i.e., on networks/configurations that have not enabled the corresponding hard fork switch. Where that switch is enabled, the optimized `delegate()`/`convert()` methods use prefixed per-pair keys instead of a single growing list, which does not have this specific growth-and-rescan pattern (though `convert()` itself still iterates any pre-existing legacy list once per address). This makes the issue conditional on chain configuration rather than universally exploitable on all java-tron deployments today, which is why I present it with the caveat that it is only reachable pre-hardfork/on chains still exercising the legacy branch.

### Recommendation
Deprecate/remove the legacy `!supportAllowDelegateOptimization()` branch in `FreezeBalanceActuator.delegateResource()` (and audit `DelegateResourceActuator`/`DelegateResourceProcessor` for the same pattern) or, if it must remain for backward compatibility, cap the number of entries permitted in `DelegatedResourceAccountIndexCapsule.toAccountsList`/`fromAccountsList` per owner (analogous to `UNFREEZE_MAX_TIMES`/`MAX_ACTIVE_ORDER_NUM`) and replace the `List.contains()` linear de-duplication check with an O(1)/O(log n) structure (e.g., switch fully to the prefixed-key index used by `delegate()`/`getWithPrefix()`), so that per-transaction cost does not grow with the number of distinct delegation targets ever created.

### Proof of Concept
Conceptual reproduction (network must have `supportAllowDelegateOptimization()` disabled):
1. Fund one attacker-controlled `OWNER` account with enough TRX to cover repeated 1 TRX freezes and fees.
2. Generate N distinct, previously-unused receiver addresses `R_1..R_N`.
3. For each `R_i`, broadcast a `FreezeBalanceContract` from `OWNER` with `frozen_balance = 1 TRX`, `resource = BANDWIDTH`, `receiver_address = R_i`.
4. Each transaction executes `FreezeBalanceActuator.delegateResource()`, which appends `R_i` to `OWNER`'s `DelegatedResourceAccountIndexCapsule.toAccountsList` after an O(n) `contains()` scan over the list built so far — see [3](#0-2) .
5. Measure per-transaction execution/apply time for `i = 1000, 2000, 4000, ...`; observe the O(n) growth per call (and O(n²) cumulative cost) as the list grows, analogous to the `cost n` measurement in the reference report.

I was not able to fully confirm from the indexed code whether `supportAllowDelegateOptimization()` is already forced "on" for the current mainnet/latest hard-fork configuration in this repository snapshot (i.e., whether the vulnerable branch is dead code on production chains); this would need to be checked against the live `ForkController`/`ProposalUtil` hard-fork activation state before treating this as exploitable on a running mainnet node, and I recommend a full Devin session with repository access to confirm current activation status if precise applicability to a specific deployed network needs to be established.

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

**File:** chainbase/src/main/java/org/tron/core/capsule/DelegatedResourceAccountIndexCapsule.java (L57-86)
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
```
