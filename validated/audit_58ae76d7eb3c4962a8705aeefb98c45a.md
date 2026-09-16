### Title
Unbounded delegate/undelegate index list growth causes gas-exhaustion revert on migration `convert()` - (File: chainbase/src/main/java/org/tron/core/store/DelegatedResourceAccountIndexStore.java)

### Summary
The reported java-tron analog is the unbounded `toAccountsList`/`fromAccountsList` maintained inside a single `DelegatedResourceAccountIndexCapsule` record by the legacy (pre-optimization) resource-delegation path. An unprivileged account can grow this list without bound by issuing many `FreezeBalanceContract` delegate transactions to distinct receiver addresses, and the list is later iterated element-by-element (in `DelegatedResourceAccountIndexStore.convert()`), mirroring the `recordMintBestAvailableTier`-style "iterate over unbounded attacker-controlled collection inside a single transaction" bug class from the source report.

### Finding Description
`DelegatedResourceAccountIndexCapsule` stores two protobuf-repeated fields, `toAccountsList` and `fromAccountsList`, keyed by a single owner/receiver address. [1](#0-0) 

When `DynamicPropertiesStore.supportAllowDelegateOptimization()` is disabled (the legacy code path, still present and reachable for any account/chain state where this flag has not converted the index), `FreezeBalanceActuator.delegateResource` appends a new entry to the owner's `toAccountsList` and to the receiver's `fromAccountsList` for every distinct receiver address the owner delegates to, with no upper bound on the number of distinct receivers: [2](#0-1) 

This list is later iterated in full by `DelegatedResourceAccountIndexStore.convert()`, which is invoked (via `delegatedResourceAccountIndexStore.convert(...)`) from the same actuator family (`FreezeBalanceActuator`/`UnfreezeBalanceActuator`) once the optimized delegate model is enabled, in order to migrate all legacy entries for an address into the new prefix-keyed model: [3](#0-2) 

Because the size of `toList`/`fromList` is entirely attacker-controlled (an unprivileged sender can create arbitrarily many `FreezeBalanceContract` delegations to unique receiver addresses before the migration flag flips, or before the address is first touched under the optimized model), the `convert()` call executed inside a normal user transaction can be forced to loop over an unbounded number of entries, each iteration performing a further store write (`this.delegate(...)`) — directly analogous to `recordMintBestAvailableTier` looping over an attacker-inflated tier list until the transaction runs out of energy.

### Impact Explanation
If an attacker (or any user, maliciously or not) accumulates a very large number of unique delegate relationships for one address under the legacy model, the first transaction that touches that address after the optimized model activates will need to run `convert()`, which iterates the entire `toAccountsList`/`fromAccountsList` and performs a `put` for each entry. A sufficiently large list can exceed the per-transaction energy/CPU budget, causing that transaction (and any subsequent delegate/undelegate transaction for that address, since the migration is retried until it completes) to permanently and deterministically fail. This can effectively freeze the ability of the affected account to unfreeze/delegate/undelegate resources — a denial-of-service on normal staking operations for the targeted address, satisfying the "node crash or halt" / "permanent freezing of funds"-adjacent impact class (frozen TRX/resources become unusable because the delegate/undelegate transaction can never complete).

### Likelihood Explanation
Likelihood is moderate: it requires the legacy delegate model to still be in use for the target address (i.e., `supportAllowDelegateOptimization()` not yet applied to that address) and requires the attacker to generate many unique receiver addresses, which costs one `FreezeBalanceContract` transaction (with real TRX being frozen) per receiver — this adds real cost but no privileged access is needed, and any account can inflict this on itself or be targeted since receiver addresses do not need to be owned by anyone in particular for the index construction (the actuator only requires the receiver address to exist as an account). The main uncertainty is whether `supportAllowDelegateOptimization()` is already permanently enabled on the current chain state referenced by this repo snapshot, which would make the legacy branch effectively dead code going forward — this could not be fully confirmed from the available context.

### Recommendation
- Cap the number of distinct delegate relationships tracked per address in the legacy `DelegatedResourceAccountIndexCapsule` (reject new delegations once a per-account maximum is reached), mirroring the bound already applied elsewhere (e.g., `VoteWitnessActuator`'s `MAX_VOTE_NUMBER`).
- Make `DelegatedResourceAccountIndexStore.convert()` incremental/paginated (convert a bounded number of entries per transaction and track migration progress) instead of converting the full list in one shot.
- Alternatively, charge energy proportional to the list size before executing `convert()`, so the cost is paid by the account that created the large list rather than causing an unconditional revert for a legitimate later transaction.

### Proof of Concept
1. Under the legacy (non-optimized) delegate model, attacker account `A` repeatedly sends `FreezeBalanceContract` transactions with `receiverAddress` set to N distinct existing accounts, each delegating the minimum resource amount. Each transaction appends one entry to `A`'s `toAccountsList` via `FreezeBalanceActuator.delegateResource` [2](#0-1) .
2. Once N becomes large enough that a single `convert()` pass exceeds the block/transaction energy limit, any transaction that triggers `DelegatedResourceAccountIndexStore.convert(A)` — such as a subsequent `UnfreezeBalanceActuator`/`FreezeBalanceActuator`/`DelegateResourceActuator` operation on `A` after the optimized model is enabled — will loop over the full `toList`/`fromList` inside `convert()` [3](#0-2)  and run out of gas/energy, causing the transaction to fail deterministically every time it is retried, since `convert()` re-attempts the full list on each call until it succeeds.

### Citations

**File:** chainbase/src/main/java/org/tron/core/capsule/DelegatedResourceAccountIndexCapsule.java (L46-61)
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
