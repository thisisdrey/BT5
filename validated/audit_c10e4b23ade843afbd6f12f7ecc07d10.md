## Title
Unbounded growth of `DelegatedResourceAccountIndex` lists causes O(n) linear scans and full-list rewrites on every freeze/unfreeze, enabling a single account to degrade node performance - (`File: actuator/src/main/java/org/tron/core/actuator/FreezeBalanceActuator.java`)

### Summary
`CVE-2021-28698` describes Xen tying up a CPU because it iterates a grant-mapping table that can grow unboundedly through legitimate but unbounded guest activity (a domain mapping its own or a cooperating domain's grants). The equivalent pattern in java-tron is the legacy (`!supportAllowDelegateOptimization()`) `DelegatedResourceAccountIndex` bookkeeping: an account that repeatedly issues `FreezeBalanceContract` (bandwidth/energy delegation) to distinct receiver addresses appends one entry per receiver to an unbounded `toAccounts`/`fromAccounts` protobuf-repeated-field list, with no cap on the number of entries, and every single delegate/undelegate operation does a full linear scan plus a full copy/rebuild of that list.

### Finding Description
In `FreezeBalanceActuator.delegateResource()`, when `dynamicPropertiesStore.supportAllowDelegateOptimization()` is false (the legacy resource-delegation index model, which is the default until the SR committee enables the optimization proposal), each freeze-with-receiver transaction does: [1](#0-0) 

```
List<ByteString> toAccountsList = ownerIndexCapsule.getToAccountsList();
if (!toAccountsList.contains(ByteString.copyFrom(receiverAddress))) {
  ownerIndexCapsule.addToAccount(ByteString.copyFrom(receiverAddress));
}
delegatedResourceAccountIndexStore.put(ownerAddress, ownerIndexCapsule);
...
List<ByteString> fromAccountsList = receiverIndexCapsule.getFromAccountsList();
if (!fromAccountsList.contains(ByteString.copyFrom(ownerAddress))) {
  receiverIndexCapsule.addFromAccount(ByteString.copyFrom(ownerAddress));
}
```

`contains()` is a linear scan over the entire list, and `addToAccount()`/`addFromAccount()` rebuild the whole protobuf message via `toBuilder().addToAccounts(toAccount).build()`: [2](#0-1) 

Because there is no maximum size check on `toAccountsList`/`fromAccountsList` (unlike, e.g., the market-order actuator's `MAX_ACTIVE_ORDER_NUM` cap), a single account can drive this list to an arbitrary size simply by issuing one `FreezeBalanceContract` per distinct new receiver address it controls. Every subsequent freeze/unfreeze touching that owner or receiver then:
1. Fully deserializes the growing protobuf capsule from the store (`delegatedResourceAccountIndexStore.get`),
2. Performs an O(n) `contains()` scan,
3. Rebuilds and re-serializes the entire list on `put()`.

The same O(n) pattern repeats on `UnfreezeBalanceActuator`, which copies the whole list into an `ArrayList`, calls `.remove()` (another O(n) scan), and rewrites the full list: [3](#0-2) 

This mirrors the Xen bug class exactly: a resource-index table that legitimate (non-malicious, single-account) usage can grow without bound, and whose per-operation cost (iteration, in-use-entry handling) scales linearly with the number of accumulated entries rather than being bounded — tying up processing time for every subsequent transaction that touches the same owner/receiver index entry, and bloating the on-disk/serialized value that must be read and rewritten by the block-applying node on each transaction.

### Impact Explanation
Each `FreezeBalanceContract` to a new receiver address costs only the transaction's fixed bandwidth/energy fee, but drives O(n) work in `Manager`'s actuator execution during block application. An attacker who freezes trivial amounts of TRX to thousands of distinct receiver addresses they control can grow their own `DelegatedResourceAccountIndexCapsule` (and the corresponding receivers' `fromAccounts` lists) to a large size cheaply. Subsequent freeze/unfreeze/delegate operations touching that account become increasingly expensive to process (deserialize/scan/reserialize a large list) for every full node validating the block, which can slow down block application and event processing across the whole network — a resource-exhaustion/performance-degradation condition consistent with the CVE's CVSS profile (availability impact, no confidentiality/integrity impact).

### Likelihood Explanation
The vulnerable path is only active while `AllowDelegateOptimization` (a proposal-gated dynamic property) is disabled; once enabled, `FreezeBalanceActuator` uses the more scalable per-key `delegate()`/`unDelegate()` index construction that avoids the unbounded list. On networks/deployments where this proposal has not been activated (private chains, some historical mainnet periods, or any java-tron based network that hasn't voted the optimization on), any unprivileged, non-witness account can trigger this by broadcasting ordinary `FreezeBalanceContract` transactions with distinct receiver addresses — no special privileges, cooperation from an SR, or malicious peer/node behavior is required.

### Recommendation
- Impose a hard cap on the number of entries in `DelegatedResourceAccountIndexCapsule.toAccountsList`/`fromAccountsList` (similar to `MarketSellAssetActuator`'s `MAX_ACTIVE_ORDER_NUM`), rejecting further delegation once the cap is reached, or
- Force the per-key delegated-resource index model (`convert`/`delegate`/`unDelegate`, keyed by `(from, to)` pairs) unconditionally, removing the legacy unbounded-list code path entirely, and
- Avoid whole-list rebuild/copy operations (`toBuilder().addToAccounts()`, `ArrayList` copy + `remove()`) in favor of amortized-O(1) index mutations.

### Proof of Concept
1. On a network where `AllowDelegateOptimization` has not been enabled (default `0`), have account `A` generate `N` (e.g., 50,000) fresh receiver addresses `R1..RN`.
2. For each `Ri`, broadcast a `FreezeBalanceContract` from `A` with `receiver_address = Ri` and a small `frozen_balance` (e.g., 1,000,000 sun) and `resource = BANDWIDTH`.
3. Each transaction is individually valid and cheap, but grows `A`'s `DelegatedResourceAccountIndexCapsule.toAccountsList` by one entry via [4](#0-3) .
4. After the list reaches tens of thousands of entries, every further `FreezeBalanceContract`/`UnfreezeBalanceContract` touching account `A` (from any account, since delegation is a public action) now costs O(N) to execute due to repeated `contains()`/`remove()` scans and full protobuf serialization/deserialization of the growing capsule, measurably increasing per-transaction processing time for all full nodes validating blocks containing these transactions.

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

**File:** chainbase/src/main/java/org/tron/core/capsule/DelegatedResourceAccountIndexCapsule.java (L82-86)
```java
  public void addToAccount(ByteString toAccount) {
    this.delegatedResourceAccountIndex = this.delegatedResourceAccountIndex.toBuilder()
        .addToAccounts(toAccount)
        .build();
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
