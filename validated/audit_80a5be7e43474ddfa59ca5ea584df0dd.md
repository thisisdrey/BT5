### Title
Unbounded growth of `DelegatedResourceAccountIndexCapsule.toAccounts`/`fromAccounts` causes gas/CPU DoS during legacy-to-V2 index migration - (File: `chainbase/src/main/java/org/tron/core/store/DelegatedResourceAccountIndexStore.java`)

### Summary
When `AllowDelegateOptimization` is disabled, `FreezeBalanceActuator.delegateResource()` appends an entry to a per-account `toAccounts`/`fromAccounts` list on every new distinct delegation, with no bound on list size. When the network later enables `AllowDelegateOptimization`, every subsequent freeze/delegate call for that same address invokes `DelegatedResourceAccountIndexStore.convert()`, which iterates over the *entire* legacy `toAccounts`/`fromAccounts` list and performs a `delegate()` write for every entry in a single transaction — mirroring the `getLockedFunds()` unbounded-loop-over-attacker-grown-array pattern from the report.

### Finding Description
`FreezeBalanceActuator.delegateResource()` builds up `DelegatedResourceAccountIndexCapsule` lists cheaply and without any cap: [1](#0-0) 

Each call with a distinct `receiverAddress` appends one more entry to `toAccountsList`/`fromAccountsList` via `addToAccount()`/`addFromAccount()`: [2](#0-1) 

Later, `DelegatedResourceAccountIndexStore.convert()` is invoked (from the same actuator, once `supportAllowDelegateOptimization()` is true) and iterates the whole `toList`/`fromList` for the address, doing an individual DB write (`delegate()`) per entry: [3](#0-2) 

This is structurally the same bug class as the reported `BountyCore.getLockedFunds()` issue: a single unprivileged actor can cheaply grow an array tied to a specific account (here, by repeatedly freezing tiny TRX amounts and delegating to N distinct throwaway receiver addresses — no minimum-amount floor is enforced on delegation count, only on total balance), and a later, unavoidable operation (`convert()`, triggered transparently inside `delegateResource()`) must iterate that entire array in one transaction, with no pagination or gas-safety cap comparable to `MAX_MATCH_NUM` used in the market-order matching engine (`MarketSellAssetActuator`) or `MAX_VOTE_NUMBER` used in `VoteWitnessActuator`.

### Impact Explanation
If an account (or an address an attacker gets others to delegate to/from) accumulates a sufficiently large legacy `toAccounts`/`fromAccounts` list, any subsequent `FreezeBalanceContract` transaction touching that address (owner or receiver) will trigger `convert()` and attempt to write one entry per list item within a single transaction/energy budget, which can exceed block energy/time limits and fail deterministically, permanently blocking that account from freezing/delegating further resources through this path. This matches the "permanent freezing of funds/functionality" impact bar, analogous to `refundDeposit` being permanently unusable.

### Likelihood Explanation
Likelihood is constrained by the fact that this legacy branch only executes `while (!dynamicPropertiesStore.supportAllowDelegateOptimization())`; on networks (including current mainnet) where `AllowDelegateOptimization` has already been activated via proposal, new appends to the legacy list no longer occur, and existing legacy lists were capped by whatever activity occurred before the fork. I could not verify from the indexed code whether any pre-existing mainnet accounts currently hold an oversized legacy list, nor find an explicit size cap on `toAccountsList`/`fromAccountsList` anywhere in `FreezeBalanceActuator` or `DelegatedResourceAccountIndexCapsule`. This significantly limits current exploitability versus the original Solidity report (where the vulnerable path is always live), so likelihood should be treated as low-to-moderate and contingent on network configuration/history rather than confirmed exploitable today.

### Recommendation
Add an explicit cap on `toAccountsList`/`fromAccountsList` length in `FreezeBalanceActuator.delegateResource()` (reject or require `AllowDelegateOptimization` before allowing further growth), and bound the number of entries processed per `convert()` call (e.g., paginate migration across multiple blocks/transactions) so a single transaction cannot be forced to iterate an attacker-inflated list.

### Proof of Concept
1. On a network/height where `supportAllowDelegateOptimization()` is `false`, have account `A` freeze a small `frozenBalance` (e.g. minimum allowed) and delegate to N distinct freshly-generated receiver addresses `R1..RN`, each via a separate `FreezeBalanceContract` transaction — each call appends to `A`'s `toAccountsList` per `FreezeBalanceActuator.delegateResource()` (`actuator/src/main/java/org/tron/core/actuator/FreezeBalanceActuator.java:319-332`) with no upper bound.
2. Once `AllowDelegateOptimization` becomes active, submit any further `FreezeBalanceContract`/delegate transaction touching account `A`; this triggers `delegatedResourceAccountIndexStore.convert(A)` (`chainbase/src/main/java/org/tron/core/store/DelegatedResourceAccountIndexStore.java:349-350` in the actuator, `convert()` itself at lines 42-61), which loops over all N entries performing N writes in one transaction.
3. For sufficiently large N, this transaction exceeds available energy/time and reverts deterministically, blocking legitimate future freeze/delegate operations for account `A`.

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
