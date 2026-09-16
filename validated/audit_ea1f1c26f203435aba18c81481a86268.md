### Title
DoS via unbounded growth of `DelegatedResourceAccountIndexCapsule.toAccountsList`/`fromAccountsList` when resource-delegation optimization is disabled - (File: `actuator/src/main/java/org/tron/core/actuator/FreezeBalanceActuator.java`)

### Summary
This is analogous to the reported Carapace bug class: an unbounded, attacker-growable list that is fully loaded, iterated/serialized on every subsequent operation touching the affected account, enabling a resource-exhaustion attack from ordinary, unprivileged transactions. In java-tron, when the legacy (non-optimized) delegation-index code path is active, each call to `FreezeBalanceActuator.delegateResource()` appends the receiver address to the owner's `DelegatedResourceAccountIndexCapsule.toAccountsList` (and vice versa for `fromAccountsList`) with **no upper bound check**, unlike the sibling `UnfreezeBalanceV2Actuator`, which explicitly caps the analogous `unfrozenV2List` growth via `UNFREEZE_MAX_TIMES = 32`.

### Finding Description
`FreezeBalanceActuator.delegateResource()` reads the owner's and receiver's `DelegatedResourceAccountIndexCapsule`, and if the target address is not already present, appends it to the list and writes the entire capsule back to the store: [1](#0-0) 

The capsule itself has no cap on list size — `addToAccount`/`addFromAccount` unconditionally append: [2](#0-1) 

By contrast, when `UnfreezeBalanceV2Actuator` handles the structurally similar `unfrozenV2List` growth, the codebase enforces an explicit limit (`UNFREEZE_MAX_TIMES`) at validation time: [3](#0-2) 

No equivalent check exists for the delegate/index list. Because `DelegatedResourceAccountIndexCapsule` is persisted as a single serialized protobuf value under one DB key (`createDbKey()` = the account address) rather than one key per delegation, every future `put`/`get` on that key deserializes, mutates, and re-serializes the *entire* list. An attacker who repeatedly calls `FreezeBalanceActuator` (delegating tiny frozen amounts to many distinct, freshly generated receiver addresses) can grow their own `toAccountsList` (and each victim receiver's `fromAccountsList`) without bound, since each entry only costs the minimum freeze amount and a normal transaction fee — cheap and repeatable, just like `buyProtection()` in the original report.

This oversized capsule is subsequently read and fully iterated by unfreeze processing (`UnfreezeBalanceActuator`/`UnfreezeBalanceProcessor`), which reconstructs and mutates the same lists: [4](#0-3) 

It is also fully returned by the query API path (`GetDelegatedResourceAccountIndex`), which any anonymous HTTP/gRPC client can invoke, forcing the node to serialize the bloated list on every request: [5](#0-4) 

### Impact Explanation
Repeated growth of this single-key list increases per-transaction CPU/serialization cost for every subsequent freeze/unfreeze/delegate operation involving the attacker's (or victims') address, and increases the cost of servicing the `GetDelegatedResourceAccountIndex` query API for that address. This degrades node processing time for a specific account's transactions and for API responses, matching the "node can no longer serve" / resource-exhaustion impact class from the original report, reachable purely via unprivileged, self-funded transactions (`FreezeBalanceContract`) plus anonymous API calls.

### Likelihood Explanation
The legacy code path is gated by the dynamic property `supportAllowDelegateOptimization` — I could not confirm from the available index whether this flag is enabled by default on the current mainnet/production configuration of this repo version. If the optimized path (separate DB keys per delegation via `delegate()`/`unDelegate()`) is the active default, this specific unbounded-single-capsule growth is mitigated for the write path, though the optimized path's `getWithPrefix()` query still performs an unbounded `prefixQuery` scan and in-memory list construction proportional to attacker-created delegation count, which remains a lesser-severity read-side cost concern. Given this uncertainty about the currently-active code path, exact severity is difficult to confirm without further live-configuration verification.

### Recommendation
- Enforce a maximum count on `toAccountsList`/`fromAccountsList` entries per account in `FreezeBalanceActuator`/`DelegateResourceActuator` validate(), mirroring the `UNFREEZE_MAX_TIMES` pattern used in `UnfreezeBalanceV2Actuator`.
- For the optimized path, bound or paginate `getWithPrefix()`'s `prefixQuery` result set, and cap results returned by `GetDelegatedResourceAccountIndex`/`GetDelegatedResourceAccountIndexV2` APIs.

### Proof of Concept
1. Attacker account A freezes a minimal amount of TRX for BANDWIDTH/ENERGY and delegates it to a newly-generated receiver address R1 via `FreezeBalanceContract` (delegated form), causing `FreezeBalanceActuator.delegateResource()` to append R1 to A's `toAccountsList`.
2. Repeat step 1 thousands of times with freshly generated receiver addresses R2..Rn, each delegation being a distinct, cheap, valid transaction.
3. A's `DelegatedResourceAccountIndexCapsule` under key A now contains n entries in `toAccountsList`; each receiver Ri's capsule under key Ri contains A in `fromAccountsList`.
4. Any subsequent transaction touching A's delegation state (freeze/unfreeze/delegate/undelegate) or a `GetDelegatedResourceAccountIndex` query for A must fully deserialize/serialize this oversized list, increasing per-call CPU/latency for that account and query path.

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

**File:** actuator/src/main/java/org/tron/core/actuator/UnfreezeBalanceV2Actuator.java (L179-182)
```java
    int unfreezingCount = accountCapsule.getUnfreezingV2Count(now);
    if (UNFREEZE_MAX_TIMES <= unfreezingCount) {
      throw new ContractValidateException("Invalid unfreeze operation, unfreezing times is over limit");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/UnfreezeBalanceActuator.java (L163-188)
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
        } else {
          //modify DelegatedResourceAccountIndexStore new
          delegatedResourceAccountIndexStore.convert(ownerAddress);
          delegatedResourceAccountIndexStore.convert(receiverAddress);
          delegatedResourceAccountIndexStore.unDelegate(ownerAddress, receiverAddress);
        }
```

**File:** framework/src/main/java/org/tron/core/services/http/GetDelegatedResourceAccountIndexServlet.java (L1-2)
```java
package org.tron.core.services.http;

```
