## Title
Unbounded iteration in `DelegatedResourceAccountIndexStore.convert()` triggered by `FreezeBalanceActuator`/`UnfreezeBalanceActuator` can exceed the energy/gas limit for accounts with large legacy delegation indexes - (File: chainbase/src/main/java/org/tron/core/store/DelegatedResourceAccountIndexStore.java)

### Summary
The external report describes SKALE's `_generateGroup`, which iterates over the entire (network-wide, unbounded) list of nodes inside a single transaction, risking a block gas-limit revert as the collection grows. The analogous pattern in java-tron is `DelegatedResourceAccountIndexStore.convert()`, which iterates over the full `toAccountsList`/`fromAccountsList` of the legacy (pre-optimization) `DelegatedResourceAccountIndex` for a single account and re-writes every entry to the new key-per-relationship model. This loop's cost scales with however many delegation relationships accumulated for that address under the old model, and it is invoked synchronously inside ordinary, unprivileged user transactions.

### Finding Description
`convert()` walks the entire legacy index arrays and performs two DB writes (`put`) per entry: [1](#0-0) 

This method is called directly from actuator business logic that any account holder can trigger — `FreezeBalanceActuator.delegateResource()` calls `convert(ownerAddress)` and `convert(receiverAddress)` before writing the new-style delegation record: [2](#0-1) 

and `UnfreezeBalanceActuator` calls the same `convert()` pair when releasing a legacy delegated resource: [3](#0-2) 

Prior to `supportAllowDelegateOptimization()` being enabled, every `FreezeBalanceContract`/`UnfreezeBalanceContract` with delegation appended to the old-style, unbounded `toAccountsList`/`fromAccountsList` arrays on `DelegatedResourceAccountIndexCapsule` (any address could be added as a "to"/"from" account by any counterpart performing a delegated freeze): [4](#0-3) [5](#0-4) 

Because these lists have no explicit size cap, an address that participated in many delegated-freeze relationships before the optimization flag flipped can end up with an arbitrarily large legacy index. Any later transaction on that address that goes through `delegateResource()` (i.e., another delegated `FreezeBalanceContract`, or an `UnfreezeBalanceContract` releasing a legacy delegation) forces `convert()` to iterate that entire historical list and perform 2 store writes per element — all inside the energy/CPU budget of one transaction, exactly as `_generateGroup` iterates over `possibleNodes` inside one Ethereum transaction.

### Impact Explanation
If the legacy index for an owner or receiver address is large enough, the single-transaction work done by `convert()` (iterating the array and performing `2 * size` store writes) can exceed the transaction's energy limit. This means:
- The specific `FreezeBalanceContract`/`UnfreezeBalanceContract` transaction touching that address permanently fails (it can never complete because the fixed-cost conversion work always exceeds the available energy), denying that account's ability to freeze/unfreeze delegated resources — a resource/service-availability impact for the affected account.
- Because `convert()` also performs many `put()` writes, an unusually large legacy index inflates the resource cost of processing that transaction for every node validating the block, which is the same "state growth causing per-transaction cost blow-up" class of issue flagged in the SKALE report.

This falls under "node crash or halt" / "an API the node can no longer serve" for the affected account's freeze/unfreeze-delegate functionality, since the operation becomes permanently unexecutable once the legacy list exceeds the energy budget.

### Likelihood Explanation
The precondition (a very large legacy `toAccountsList`/`fromAccountsList`) can only be built up while `supportAllowDelegateOptimization()` was still disabled (a chain-wide committee-controlled parameter), so this is a legacy/pre-existing-state risk rather than something an attacker can freely grow at will today. However, once such state exists for an address, invoking `convert()` on it is fully reachable by an ordinary unprivileged user broadcasting a `FreezeBalanceContract` (with a `receiverAddress`) or `UnfreezeBalanceContract`, requiring no special privilege — matching the "Pending / next few months improvement" severity assessment SKALE gave to the analogous bug.

### Recommendation
Avoid unbounded iteration over legacy relationship lists inside a single transaction's execution path:
- Cap and/or amortize the `convert()` work (e.g., convert a limited number of entries per call, or convert lazily entry-by-entry only for the specific counterpart being interacted with) instead of converting the complete historical list at once.
- Alternatively, precompute/backfill the migration to the new key-per-relationship model via an offline/administrative migration path rather than doing it inline during user transactions, so per-transaction cost stays O(1) regardless of historical size.

### Proof of Concept
1. Before `AllowDelegateOptimization` is enabled on the chain, have address `A` participate as `receiverAddress` in `N` distinct delegated `FreezeBalanceContract` transactions from `N` different owner addresses (each unprivileged, ordinary transaction), causing `A`'s legacy `DelegatedResourceAccountIndexCapsule.fromAccountsList` to grow to size `N` via `FreezeBalanceActuator.delegateResource()`.
2. Once `AllowDelegateOptimization` is enabled, submit any `FreezeBalanceContract` (with delegation) or `UnfreezeBalanceContract` involving address `A` as owner or receiver.
3. `delegateResource()`/execution path calls `delegatedResourceAccountIndexStore.convert(A)`, which loops over all `N` legacy entries, each entry causing two `put()` operations.
4. For sufficiently large `N`, this single transaction's energy consumption exceeds the block/transaction energy limit, causing the transaction to permanently fail for address `A`, effectively freezing/unfreezing delegated-resource functionality for that account.

### Citations

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

**File:** actuator/src/main/java/org/tron/core/actuator/UnfreezeBalanceActuator.java (L183-188)
```java
        } else {
          //modify DelegatedResourceAccountIndexStore new
          delegatedResourceAccountIndexStore.convert(ownerAddress);
          delegatedResourceAccountIndexStore.convert(receiverAddress);
          delegatedResourceAccountIndexStore.unDelegate(ownerAddress, receiverAddress);
        }
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
