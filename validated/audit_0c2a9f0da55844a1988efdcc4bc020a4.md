Found it: `DelegatedResourceAccountIndexCapsule` maintains legacy `FromAccounts`/`ToAccounts` lists that grow unbounded through the `FreezeBalanceActuator` legacy delegation path, with no cap on how many distinct receivers/senders one address can accumulate.

### Title
Unbounded growth of legacy DelegatedResourceAccountIndex from/to-accounts list enables denial-of-service on resource delegation reads - (File: `chainbase/src/main/java/org/tron/core/capsule/DelegatedResourceAccountIndexCapsule.java`)

### Summary
When `supportAllowDelegateOptimization()` is not enabled (legacy path, still supported in the codebase), `FreezeBalanceActuator.delegateResource` appends the receiver/sender address to `DelegatedResourceAccountIndexCapsule`'s `ToAccounts`/`FromAccounts` protobuf-repeated lists every time a new counterparty is used, with no upper bound on the number of distinct entries.

### Finding Description
`FreezeBalanceActuator.delegateResource` (an unprivileged, broadcastable `FreezeBalanceContract` operation) updates the index as follows when `!dynamicPropertiesStore.supportAllowDelegateOptimization()`: [1](#0-0) 

The check only prevents duplicate entries for the *same* counterparty address (`if (!toAccountsList.contains(...))`), but places no limit on how many *distinct* counterparty addresses can be accumulated. An attacker controlling (or paying fees on) many addresses can repeatedly call `FreezeBalance` with delegation targeting one fixed victim address as the `receiverAddress`, each time from a newly generated `ownerAddress` (or vice versa), causing the victim's `DelegatedResourceAccountIndexCapsule.FromAccounts` (or `ToAccounts`) list to grow without bound: [2](#0-1) 

This unbounded list is later read in full by `DelegatedResourceAccountIndexStore.convert()`, which is invoked from the delegation path once `supportAllowDelegateOptimization` becomes active, iterating over every entry of `toList`/`fromList` and calling `this.delegate(...)` (a DB write) for each: [3](#0-2) 

`convert()` is called for both the owner and receiver address on essentially every `FreezeBalance`/`DelegateResource` legacy-index migration, meaning a single subsequent transaction touching the victim address as owner/receiver triggers this same-block iteration and write-amplification over the entire attacker-inflated list, directly mirroring the reported bug class (an ever-growing array iterated in one execution/block with no cap).

### Impact Explanation
If enough entries are accumulated, the iteration and per-entry DB writes in `convert()` performed synchronously inside a single transaction's `execute()` can grow large enough to make block processing for that transaction disproportionately expensive, degrading node performance and potentially pushing the transaction/block processing time toward node/block time budgets — a resource-exhaustion condition on a chain-processing path (Manager/actuator execution), not merely an off-chain/API-only cost.

### Likelihood Explanation
Likelihood is limited by the cost of freezing/delegating balance to generate each new distinct address (freezing requires locking TRX and paying transaction bandwidth/energy), so the attack is economically bounded but not prevented by any protocol-level maximum-list-size check — unlike the analogous `votes` list (bounded by `MAX_VOTE_NUMBER`) or `unfrozenV2` list (bounded by `UNFREEZE_MAX_TIMES = 32`), this from/to-accounts index has no such cap.

### Recommendation
Add an explicit maximum size check (similar to `MAX_VOTE_NUMBER` for votes or `UNFREEZE_MAX_TIMES` for unfreeze operations) on `DelegatedResourceAccountIndexCapsule`'s `FromAccounts`/`ToAccounts` lists in `FreezeBalanceActuator.delegateResource`'s legacy branch, or migrate/deprecate the legacy indexing path entirely (favoring the already-existing prefixed-key `V2` index in `DelegatedResourceAccountIndexStore`, which doesn't require iterating a per-account list) and disallow further legacy index growth.

### Proof of Concept
1. Ensure `AllowDelegateOptimization` is not yet activated on the chain (legacy path active).
2. Attacker repeatedly generates new key pairs and, for each, sends a `FreezeBalance` transaction (with `receiverAddress` set to a fixed victim address) after funding each address minimally to freeze balance and delegate to the victim, as in `FreezeBalanceActuator.delegateResource`, incrementing `victim.FromAccounts` each time with no cap, as shown in the existing test setup that manually constructs a 100+-entry `ToAccounts` list: `framework/src/test/java/org/tron/core/actuator/FreezeBalanceActuatorTest.java` (`testMultiFreezeDelegatedBalanceForBandwidth`, lines 276–337) — confirming the underlying data structure and code path allow unbounded accumulation without any size validation in `validate()`/`execute()`.
3. Once `AllowDelegateOptimization` is later enabled (a normal, planned protocol activation), the very next `delegateResource` call touching the victim address triggers `DelegatedResourceAccountIndexStore.convert(victimAddress)`, iterating and re-writing every one of the accumulated entries synchronously within that single transaction's execution.

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
